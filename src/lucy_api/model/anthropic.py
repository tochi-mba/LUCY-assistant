"""Claude, over its HTTP API, with no vendor SDK in the dependency graph.

An SDK would bring its own HTTP client, its own retry policy and its own exception
taxonomy, and all three would end up visible from the agent loop -- which is written
against `Provider` precisely so that it cannot be. The wire format is small, it is
stable, and writing it out is how the next person finds out what we actually depend on.

## The fields this relies on, so a break has somewhere to be looked for

**Request.** `model`, `max_tokens`, `messages`, and `system` as a *list* of one text
block carrying `cache_control: {"type": "ephemeral"}`. The list form is not decoration:
the system prompt is the stable prefix, and marking it is the difference between
`cache_read_input_tokens` being most of the input and being zero. A plan is asked for
with `output_config.format` as a `json_schema`; thinking depth is `thinking` plus
`output_config.effort`.

**Reply.** `content[]` blocks of type `text` and `thinking`, `stop_reason`, the
`stop_details` that accompanies a refusal, and `usage` with `input_tokens`,
`output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens`. Anthropic
counts cached tokens *beside* `input_tokens` rather than inside it, which is the meaning
`Usage` documents, so no arithmetic is needed here.

**Stream.** `message_start`, `content_block_delta` carrying `text_delta` and
`thinking_delta`, `message_delta` carrying the stop reason and the output token count,
`message_stop`, and `error`. Block boundaries are ignored on purpose: `Reply` keeps text
and reasoning in two fields, so where one block ended and the next began carries nothing
this seam models.

**Headers.** `x-api-key`, `anthropic-version: 2023-06-01`, and `retry-after` on a 429.

## Two things this deliberately does not do

It does not send `temperature` unless the caller set one, and current Claude models
reject sampling parameters outright. The field is passed through rather than dropped so
that an older model or a compatible endpoint still behaves as the caller asked, and a
rejection is a loud 400 rather than a setting that quietly did nothing.

It does not ask for strict schema enforcement. A compiled weftai plan schema uses
constructs strict mode refuses, and a 400 for the schema is worse than a plan we have to
validate ourselves -- which the loop does regardless, because the repair path has to
exist for every provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from lucy_api.model.types import (
    Chunk,
    ModelRefusedError,
    ModelUnavailableError,
    Reply,
    Stop,
    Usage,
)
from lucy_api.model.wire import (
    CHUNK_DONE,
    CHUNK_REASONING,
    CHUNK_TEXT,
    DEFAULT_TIMEOUT,
    ModelCallFailedError,
    as_dict,
    as_list,
    as_text,
    count,
    events,
    json_object,
    model_for,
    send,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from lucy_api.model.types import Request

BASE_URL = "https://api.anthropic.com"
MESSAGES_PATH = "/v1/messages"
API_VERSION = "2023-06-01"

EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
"""Thinking depths Anthropic names. Anything else says nothing and takes the default."""

THINKING_OFF = frozenset({"off", "none", "disabled"})

STOPS: Mapping[str, Stop] = {
    "end_turn": Stop.end_turn,
    "stop_sequence": Stop.end_turn,
    "max_tokens": Stop.max_tokens,
    "tool_use": Stop.tool_use,
    "pause_turn": Stop.tool_use,
}
"""Anthropic's stop reasons in Lucy's words.

An unknown one ends the turn. Inventing resumability is the one mistake a client must not
make: offering to continue something that cannot be continued wastes a call and a person's
patience, while treating a resumable stop as final only costs a sentence they can ask for
again.
"""

REFUSAL = "refusal"
TRANSIENT_ERRORS = frozenset({"overloaded_error", "rate_limit_error", "api_error"})


def _usage(usage: dict[str, Any]) -> Usage:
    return Usage(
        input_tokens=count(usage.get("input_tokens")),
        output_tokens=count(usage.get("output_tokens")),
        cache_read_tokens=count(usage.get("cache_read_input_tokens")),
        cache_write_tokens=count(usage.get("cache_creation_input_tokens")),
    )


def _refusal(payload: dict[str, Any]) -> str:
    details = as_dict(payload.get("stop_details"))
    category = as_text(details.get("category")) or "unspecified"
    explanation = as_text(details.get("explanation")) or "no reason was given"
    return f"anthropic declined this request ({category}): {explanation}"


def _stream_error(error: dict[str, Any]) -> Exception:
    kind = as_text(error.get("type")) or "error"
    detail = as_text(error.get("message")) or "no detail given"
    message = f"anthropic sent {kind} mid-stream: {detail}"
    if kind in TRANSIENT_ERRORS:
        return ModelUnavailableError(message)
    return ModelCallFailedError(message)


def reply_from(payload: dict[str, Any], *, want_plan: bool) -> Reply:
    """One message, streamed or not, as the seam's `Reply`.

    When a plan was asked for and parses, `text` is left empty: the JSON is the answer to
    a machine, and a client that rendered it would be showing a person the wiring. When it
    does not parse, the raw text survives, because that is what the repair turn quotes
    back to the model.
    """
    stop_reason = as_text(payload.get("stop_reason")) or "end_turn"
    if stop_reason == REFUSAL:
        raise ModelRefusedError(_refusal(payload))
    spoken: list[str] = []
    thought: list[str] = []
    for block in as_list(payload.get("content")):
        kind = as_text(as_dict(block).get("type"))
        if kind == "text":
            spoken.append(as_text(block.get("text")))
        elif kind == "thinking":
            thought.append(as_text(block.get("thinking")))
    text = "".join(spoken)
    plan = json_object(text) if want_plan else None
    stop = STOPS.get(stop_reason, Stop.end_turn)
    if plan is not None and stop is not Stop.max_tokens:
        stop = Stop.tool_use
    return Reply(
        text="" if plan is not None else text,
        plan=plan,
        reasoning="".join(thought),
        stop=stop,
        usage=_usage(as_dict(payload.get("usage"))),
        model=as_text(payload.get("model")),
    )


@dataclass(slots=True)
class _Arriving:
    """A streamed message, accumulated into the shape the one parser already reads."""

    text: str = ""
    reasoning: str = ""
    message: dict[str, Any] = field(default_factory=dict)

    def delta(self, delta: dict[str, Any]) -> Chunk | None:
        kind = as_text(delta.get("type"))
        if kind == "text_delta":
            piece = as_text(delta.get("text"))
            self.text += piece
            return Chunk(kind=CHUNK_TEXT, text=piece)
        if kind == "thinking_delta":
            piece = as_text(delta.get("thinking"))
            self.reasoning += piece
            return Chunk(kind=CHUNK_REASONING, text=piece)
        return None

    def merge(self, delta: dict[str, Any], usage: dict[str, Any]) -> None:
        """`message_delta` carries the stop reason, and the output tokens separately."""
        self.message.update(delta)
        self.message["usage"] = {**as_dict(self.message.get("usage")), **usage}

    def payload(self) -> dict[str, Any]:
        return {
            **self.message,
            "content": [
                {"type": "text", "text": self.text},
                {"type": "thinking", "thinking": self.reasoning},
            ],
        }


class AnthropicProvider:
    """Claude, over `POST /v1/messages`."""

    name: str = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._http.aclose()

    async def complete(self, request: Request) -> Reply:
        """One call, one reply."""
        payload = await send(self._http, self._request(request, stream=False), provider=self.name)
        return reply_from(payload, want_plan=request.plan_schema is not None)

    async def stream(self, request: Request) -> AsyncIterator[Chunk]:
        """The same call, delivered as it is generated.

        When a plan was asked for, the text deltas *are* the plan's JSON. A caller
        forwarding deltas to a person has to know that and stay quiet for a plan turn;
        the `done` chunk is the one that carries the parsed result either way.
        """
        want_plan = request.plan_schema is not None
        arriving = _Arriving()
        async for event in events(
            self._http, self._request(request, stream=True), provider=self.name
        ):
            kind = as_text(event.get("type"))
            if kind == "content_block_delta":
                chunk = arriving.delta(as_dict(event.get("delta")))
                if chunk is not None:
                    yield chunk
            elif kind == "message_start":
                arriving.message = as_dict(event.get("message"))
            elif kind == "message_delta":
                arriving.merge(as_dict(event.get("delta")), as_dict(event.get("usage")))
            elif kind == "message_stop":
                yield Chunk(
                    kind=CHUNK_DONE,
                    reply=reply_from(arriving.payload(), want_plan=want_plan),
                )
            elif kind == "error":
                raise _stream_error(as_dict(event.get("error")))

    def _request(self, request: Request, *, stream: bool) -> httpx.Request:
        body = self._body(request, stream=stream)
        return self._http.build_request("POST", MESSAGES_PATH, json=body)

    def _body(self, request: Request, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model_for(request, self.model),
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
        }
        if request.system:
            body["system"] = [
                {
                    "type": "text",
                    "text": request.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        output_config: dict[str, Any] = {}
        if request.plan_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": request.plan_schema}
        if request.max_thinking_tokens > 0:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": request.max_thinking_tokens,
            }
        elif request.thinking in EFFORTS:
            body["thinking"] = {"type": "adaptive", "display": "summarized"}
            output_config["effort"] = request.thinking
        elif request.thinking in THINKING_OFF:
            body["thinking"] = {"type": "disabled"}
        if output_config:
            body["output_config"] = output_config
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if stream:
            body["stream"] = True
        return body


__all__ = ["API_VERSION", "BASE_URL", "MESSAGES_PATH", "AnthropicProvider", "reply_from"]
