"""GPT, over the Responses API, with no vendor SDK in the dependency graph.

Same reasoning as the Claude adapter: the loop is written against `Provider`, and an SDK
would put its own client, retries and exception types where the loop can see them. The
Responses API is the surface used rather than chat completions, for one concrete reason
-- `instructions` is a field beside the conversation rather than a message inside it, so
"send the system prompt separately from the messages" is something this adapter can
actually honour instead of approximate.

## The fields this relies on, so a break has somewhere to be looked for

**Request.** `model`, `instructions`, `input` as a list of role/content messages,
`max_output_tokens`, `text.format` as a named `json_schema` when a plan is wanted, and
`reasoning.effort` for depth.

**Reply.** `status` (`completed`, `incomplete`, `failed`), `incomplete_details.reason`,
and `output[]` -- items of type `message`, whose `content[]` holds `output_text` and
`refusal` blocks, and items of type `reasoning`, whose `summary[]` holds `summary_text`.
`output_text` at the top level is an SDK convenience and is not read here. Usage is
`usage.input_tokens`, `usage.output_tokens` and `usage.input_tokens_details.cached_tokens`.

**Stream.** `response.output_text.delta`, `response.reasoning_summary_text.delta`, and
the terminal `response.completed` / `response.incomplete` / `response.failed`, each of
which carries the whole response object -- so the streaming path parses the reply with
exactly the same function the non-streaming path uses, and cannot drift from it.

**Headers.** `Authorization: Bearer`, and `retry-after` on a 429.

## Cached tokens are subtracted, on purpose

OpenAI counts cached tokens *inside* `input_tokens`; Anthropic counts them beside it.
`Usage` documents `cache_read` as reported separately rather than folded in, and the
whole argument for ordering the prompt by volatility rests on comparing that number
across turns and across providers. So the cached count is taken out of `input_tokens`
here, and one field means one thing whoever answered.

## Two things this deliberately does not do

It does not ask for strict schema enforcement: a compiled weftai plan schema uses
constructs strict mode refuses, and a 400 for the schema is worse than a plan we have to
validate ourselves, which the loop does regardless.

It does not ask for a reasoning summary. The provider returns one only for accounts it
has verified, so `Reply.reasoning` is usually empty here -- read, faithfully, when it is
present, and never fabricated from the visible text when it is not. Lucy's `xhigh` and
`max` are clamped to `high`, which is the deepest level this API names; the alternative
is sending nothing and silently getting *less* thinking than a person asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    model_for,
    said_and_planned,
    send,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from lucy_api.model.types import Request

BASE_URL = "https://api.openai.com"
RESPONSES_PATH = "/v1/responses"
PLAN_FORMAT_NAME = "plan"

EFFORTS: Mapping[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
"""Lucy's thinking depths in OpenAI's vocabulary. An unlisted one takes the default."""

TERMINAL_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})
TRANSIENT_CODES = frozenset({"rate_limit_exceeded", "server_error", "overloaded"})

MAX_TOKENS_REASON = "max_output_tokens"
CONTENT_FILTER_REASON = "content_filter"


@dataclass(frozen=True, slots=True)
class _Spoken:
    """What one response's output items amount to, before any of it is interpreted."""

    text: str = ""
    reasoning: str = ""
    refusal: str = ""


def _spoken(payload: dict[str, Any]) -> _Spoken:
    said: list[str] = []
    thought: list[str] = []
    refusals: list[str] = []
    for item in as_list(payload.get("output")):
        kind = as_text(as_dict(item).get("type"))
        if kind == "message":
            for block in as_list(item.get("content")):
                _read_block(as_dict(block), said, refusals)
        elif kind == "reasoning":
            thought.extend(
                as_text(as_dict(summary).get("text")) for summary in as_list(item.get("summary"))
            )
    return _Spoken(text="".join(said), reasoning="".join(thought), refusal="\n".join(refusals))


def _read_block(block: dict[str, Any], said: list[str], refusals: list[str]) -> None:
    kind = as_text(block.get("type"))
    if kind == "output_text":
        said.append(as_text(block.get("text")))
    elif kind == "refusal":
        refusals.append(as_text(block.get("refusal")))


def _usage(usage: dict[str, Any]) -> Usage:
    cached = count(as_dict(usage.get("input_tokens_details")).get("cached_tokens"))
    return Usage(
        input_tokens=max(0, count(usage.get("input_tokens")) - cached),
        output_tokens=count(usage.get("output_tokens")),
        cache_read_tokens=cached,
    )


def _stream_error(event: dict[str, Any]) -> Exception:
    code = as_text(event.get("code")) or "error"
    detail = as_text(event.get("message")) or "no detail given"
    message = f"openai sent {code} mid-stream: {detail}"
    if code in TRANSIENT_CODES:
        return ModelUnavailableError(message)
    return ModelCallFailedError(message)


def reply_from(payload: dict[str, Any], *, want_plan: bool) -> Reply:
    """One response, streamed or not, as the seam's `Reply`.

    When a plan was asked for and parses, `text` is left empty: the JSON answers a
    machine, and a client that rendered it would be showing a person the wiring. When it
    does not parse, the raw text survives, because that is what a repair turn quotes back.
    """
    if as_text(payload.get("status")) == "failed":
        error = as_dict(payload.get("error"))
        code = as_text(error.get("code")) or "no code"
        detail = as_text(error.get("message")) or "no detail given"
        msg = f"openai reported the response failed ({code}): {detail}"
        raise ModelCallFailedError(msg)
    spoken = _spoken(payload)
    reason = as_text(as_dict(payload.get("incomplete_details")).get("reason"))
    if spoken.refusal:
        msg = f"openai declined this request: {spoken.refusal}"
        raise ModelRefusedError(msg)
    if reason == CONTENT_FILTER_REASON:
        msg = "openai declined this request: the content filter stopped the response"
        raise ModelRefusedError(msg)
    said, plan = said_and_planned(spoken.text) if want_plan else (spoken.text, None)
    if reason == MAX_TOKENS_REASON:
        stop = Stop.max_tokens
    elif plan is not None:
        stop = Stop.tool_use
    else:
        stop = Stop.end_turn
    return Reply(
        text=said,
        plan=plan,
        reasoning=spoken.reasoning,
        stop=stop,
        usage=_usage(as_dict(payload.get("usage"))),
        model=as_text(payload.get("model")),
    )


class OpenAIProvider:
    """GPT, over `POST /v1/responses`."""

    name: str = "openai"

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
                "authorization": f"Bearer {api_key}",
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
        async for event in events(
            self._http, self._request(request, stream=True), provider=self.name
        ):
            kind = as_text(event.get("type"))
            if kind == "response.output_text.delta":
                yield Chunk(kind=CHUNK_TEXT, text=as_text(event.get("delta")))
            elif kind == "response.reasoning_summary_text.delta":
                yield Chunk(kind=CHUNK_REASONING, text=as_text(event.get("delta")))
            elif kind in TERMINAL_EVENTS:
                yield Chunk(
                    kind=CHUNK_DONE,
                    reply=reply_from(as_dict(event.get("response")), want_plan=want_plan),
                )
            elif kind == "error":
                raise _stream_error(event)

    def _request(self, request: Request, *, stream: bool) -> httpx.Request:
        body = self._body(request, stream=stream)
        return self._http.build_request("POST", RESPONSES_PATH, json=body)

    def _body(self, request: Request, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model_for(request, self.model),
            "max_output_tokens": request.max_output_tokens,
            "input": [
                {"role": message.role.value, "content": message.content}
                for message in request.messages
            ],
        }
        if request.system:
            body["instructions"] = request.system
        if request.plan_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": PLAN_FORMAT_NAME,
                    "schema": request.plan_schema,
                }
            }
        effort = EFFORTS.get(request.thinking)
        if effort is not None:
            body["reasoning"] = {"effort": effort}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if stream:
            body["stream"] = True
        return body


__all__ = ["BASE_URL", "RESPONSES_PATH", "OpenAIProvider", "reply_from"]
