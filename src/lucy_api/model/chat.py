"""Everybody else, over `POST /chat/completions`.

The chat-completions format is the one OpenAI shipped first and the rest of the industry
copied: the Chinese labs, the inference hosts, every local runtime. One adapter speaks it,
and the catalogue row says which provider it is talking to and how that provider deviates.

## The fields this relies on, so a break has somewhere to be looked for

**Request.** `model`, `messages` as role/content pairs with the system prompt as the first
`system` message, `max_tokens` (several providers require it), `temperature`,
`response_format` when a plan is wanted -- `json_schema` where the provider understands a
schema, `json_object` where it only understands JSON -- and `reasoning_effort` where the
provider accepts it. Streaming adds `stream` and asks for usage in the last chunk.

**Reply.** `choices[0].message.content` (a string, or a list of text parts), an optional
`refusal`, a `finish_reason` of `stop`, `length`, `content_filter` or `tool_calls`, and
`usage.prompt_tokens` / `completion_tokens` with `prompt_tokens_details.cached_tokens`.
DeepSeek and Moonshot put the model's thinking in `message.reasoning_content`; a few
others use `reasoning`. Both are read.

**Stream.** `chat.completion.chunk` objects on `data:` lines, `choices[0].delta.content`
and `delta.reasoning_content`, a chunk with `finish_reason` set, optionally a final chunk
carrying `usage`, then the `[DONE]` sentinel. An `error` object mid-stream is the failure
it describes.

## Cached tokens are subtracted, on purpose

This family counts cached tokens *inside* `prompt_tokens`, as OpenAI does. `Usage` reports
`cache_read` beside the input count rather than folded in, so the cached count is taken out
here and one field means one thing whoever answered.

## A plan through a provider that cannot take a schema

When the provider only understands "answer in JSON" the schema still has to reach the
model, so it is appended to the system prompt in words. That is a weaker guarantee than a
schema the provider enforces, and the loop already validates every plan regardless; what
it costs is a repair round now and then, which is cheaper than losing the provider.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from lucy_api.model.catalogue import Auth, Binding, ProviderSpec
from lucy_api.model.types import Chunk, ModelRefusedError, ModelUnavailableError, Reply, Stop, Usage
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

COMPLETIONS_PATH = "/chat/completions"
PLAN_FORMAT_NAME = "plan"

EFFORTS: Mapping[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}
"""Lucy's thinking depths in the chat dialect's vocabulary. An unlisted one takes the default."""

MAX_TOKENS_REASON = "length"
CONTENT_FILTER_REASON = "content_filter"
TRANSIENT_CODES = frozenset(
    {"rate_limit_exceeded", "server_error", "overloaded", "overloaded_error"}
)

SCHEMA_IN_WORDS = (
    "\n\nAnswer with a single JSON object and nothing else. It must match this JSON "
    "schema exactly:\n"
)


def _headers(spec: ProviderSpec, api_key: str) -> dict[str, str]:
    """The auth header the catalogue row asks for. A local runtime gets none."""
    headers = {"content-type": "application/json"}
    if spec.auth is Auth.bearer:
        headers["authorization"] = f"Bearer {api_key}"
    elif spec.auth is Auth.x_api_key:
        headers["x-api-key"] = api_key
    elif spec.auth is Auth.api_key_header:
        headers["api-key"] = api_key
    return headers


def _content_text(content: object) -> str:
    """Message content as one string, whether it came as text or as a list of parts."""
    if isinstance(content, str):
        return content
    return "".join(
        as_text(as_dict(part).get("text"))
        for part in as_list(content)
        if as_text(as_dict(part).get("type")) in {"text", ""}
    )


def _usage(usage: dict[str, Any]) -> Usage:
    cached = count(as_dict(usage.get("prompt_tokens_details")).get("cached_tokens"))
    return Usage(
        input_tokens=max(0, count(usage.get("prompt_tokens")) - cached),
        output_tokens=count(usage.get("completion_tokens")),
        cache_read_tokens=cached,
    )


def _stream_error(provider: str, error: dict[str, Any]) -> Exception:
    code = as_text(error.get("code")) or as_text(error.get("type")) or "error"
    detail = as_text(error.get("message")) or "no detail given"
    message = f"{provider} sent {code} mid-stream: {detail}"
    if code in TRANSIENT_CODES:
        return ModelUnavailableError(message)
    return ModelCallFailedError(message)


def reply_from(payload: dict[str, Any], *, provider: str, want_plan: bool) -> Reply:
    """One completion as the seam's `Reply`.

    When a plan was asked for and parses, `text` is left empty: the JSON answers a machine.
    When it does not parse, the raw text survives, because that is what a repair round
    quotes back.
    """
    choices = as_list(payload.get("choices"))
    first = as_dict(choices[0]) if choices else {}
    message = as_dict(first.get("message"))
    refusal = as_text(message.get("refusal"))
    if refusal:
        msg = f"{provider} declined this request: {refusal}"
        raise ModelRefusedError(msg)
    reason = as_text(first.get("finish_reason"))
    if reason == CONTENT_FILTER_REASON:
        msg = f"{provider} declined this request: the content filter stopped the response"
        raise ModelRefusedError(msg)
    text = _content_text(message.get("content"))
    reasoning = as_text(message.get("reasoning_content")) or as_text(message.get("reasoning"))
    plan = json_object(text) if want_plan else None
    if reason == MAX_TOKENS_REASON:
        stop = Stop.max_tokens
    elif plan is not None:
        stop = Stop.tool_use
    else:
        stop = Stop.end_turn
    return Reply(
        text="" if plan is not None else text,
        plan=plan,
        reasoning=reasoning,
        stop=stop,
        usage=_usage(as_dict(payload.get("usage"))),
        model=as_text(payload.get("model")),
    )


class ChatProvider:
    """One catalogued provider, over the chat-completions dialect."""

    def __init__(
        self,
        binding: Binding,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = binding.spec.id
        self.spec = binding.spec
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=binding.url,
            timeout=timeout,
            transport=transport,
            headers=_headers(binding.spec, binding.api_key),
        )

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._http.aclose()

    async def complete(self, request: Request) -> Reply:
        """One call, one reply."""
        payload = await send(self._http, self._request(request, stream=False), provider=self.name)
        error = as_dict(payload.get("error"))
        if error:
            raise _stream_error(self.name, error)
        return reply_from(payload, provider=self.name, want_plan=request.plan_schema is not None)

    async def stream(self, request: Request) -> AsyncIterator[Chunk]:
        """The same call, delivered as it is generated.

        The `done` chunk is assembled here from what streamed past, because this dialect
        has no terminal event carrying the whole response: the last chunk carries the
        finish reason, a possible extra chunk carries usage, and then the stream ends.
        """
        want_plan = request.plan_schema is not None
        said: list[str] = []
        thought: list[str] = []
        finish = ""
        usage: dict[str, Any] = {}
        model = ""
        async for event in events(
            self._http, self._request(request, stream=True), provider=self.name
        ):
            error = as_dict(event.get("error"))
            if error:
                raise _stream_error(self.name, error)
            model = as_text(event.get("model")) or model
            if event.get("usage"):
                usage = as_dict(event.get("usage"))
            choices = as_list(event.get("choices"))
            if not choices:
                continue
            first = as_dict(choices[0])
            delta = as_dict(first.get("delta"))
            text = _content_text(delta.get("content"))
            if text:
                said.append(text)
                yield Chunk(kind=CHUNK_TEXT, text=text)
            reasoning = as_text(delta.get("reasoning_content")) or as_text(delta.get("reasoning"))
            if reasoning:
                thought.append(reasoning)
                yield Chunk(kind=CHUNK_REASONING, text=reasoning)
            finish = as_text(first.get("finish_reason")) or finish
        assembled = {
            "model": model,
            "usage": usage,
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {
                        "content": "".join(said),
                        "reasoning_content": "".join(thought),
                    },
                }
            ],
        }
        yield Chunk(
            kind=CHUNK_DONE, reply=reply_from(assembled, provider=self.name, want_plan=want_plan)
        )

    def _request(self, request: Request, *, stream: bool) -> httpx.Request:
        return self._http.build_request(
            "POST", COMPLETIONS_PATH, json=self._body(request, stream=stream)
        )

    def _body(self, request: Request, *, stream: bool) -> dict[str, Any]:
        system = request.system
        body: dict[str, Any] = {
            "model": model_for(request, self.model),
            "max_tokens": request.max_output_tokens,
        }
        if request.plan_schema is not None:
            if self.spec.traits.json_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": PLAN_FORMAT_NAME, "schema": request.plan_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
                system = system + SCHEMA_IN_WORDS + json.dumps(request.plan_schema)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.extend(
            {"role": message.role.value, "content": message.content} for message in request.messages
        )
        body["messages"] = messages
        effort = EFFORTS.get(request.thinking)
        if effort is not None and self.spec.traits.reasoning_effort:
            body["reasoning_effort"] = effort
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body


__all__ = ["COMPLETIONS_PATH", "ChatProvider", "reply_from"]
