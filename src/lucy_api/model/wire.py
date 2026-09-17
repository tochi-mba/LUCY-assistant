"""What every provider agrees about, so neither HTTP adapter has to invent it twice.

Two vendors disagree about almost everything above the transport, which is the reason
`lucy_api.model.types` exists. They agree about the transport itself: JSON over HTTPS,
server-sent events for the streaming case, `Retry-After` on a 429, and an error body with
a sentence in it. Putting that agreement here keeps each adapter to the part that is
genuinely vendor-shaped -- the request body and the reply's blocks -- and means a fix to
retry handling is a fix everywhere rather than in one of two places.

## The fourth outcome

The seam names two failures: `ModelUnavailableError` for something worth retrying and
`ModelRefusedError` for a decline the person must see. A third thing happens often enough
to need a name: a 400 for a schema the provider will not accept, a 401 for a key that has
been revoked, a 404 for a model that was retired. Retrying those burns the turn's budget,
and calling them refusals tells a person their assistant declined when it did nothing of
the sort. `ModelCallFailedError` is neither, and a loop that lets it out is behaving
correctly: the turn failed, and it failed for a reason an operator can fix.

## Retry-After is obeyed, never invented

`retry_after_seconds` returns what the provider asked for or nothing at all. A guessed
interval looks exactly like an honest one to everything downstream, and the first time it
is wrong it is wrong in the direction of hammering a service that has just asked to be
left alone. Backing off with no header to go on is the caller's decision to make, visibly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx

from lucy_api.model.types import ModelUnavailableError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from lucy_api.model.types import Request

CHUNK_TEXT = "text"
"""A piece of what the model is saying."""

CHUNK_REASONING = "reasoning"
"""A piece of what the model is thinking, where the provider shows it."""

CHUNK_DONE = "done"
"""The last chunk of a stream. It carries the assembled `Reply`, and nothing else does."""

DATA_PREFIX = "data:"

DEFAULT_TIMEOUT = 120.0
"""Seconds to wait on one model call.

Generous on purpose: a long reply at high effort genuinely takes minutes, and a timeout
that fires mid-generation costs the whole call and produces nothing to show for it.
"""

MESSAGE_LIMIT = 300
"""How much of an unparseable error body is worth quoting back inside an exception."""


class ModelCallFailedError(Exception):
    """The call could not be made, and making it again would fail the same way."""


def as_dict(value: Any) -> dict[str, Any]:
    """The object a provider sent, or an empty one, so a parser never branches on null."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """The array a provider sent, or an empty one."""
    return value if isinstance(value, list) else []


def as_text(value: Any) -> str:
    """The string a provider sent, or an empty one. An absent field is not an error."""
    return value if isinstance(value, str) else ""


def count(value: Any) -> int:
    """A token count, which providers omit whenever it would have been zero."""
    return value if isinstance(value, int) else 0


def json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in `text`, or nothing.

    Nothing is a real answer here rather than an error: it is how a plan the model wrote
    badly reaches the repair path with its text still intact.
    """
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def model_for(request: Request, fallback: str) -> str:
    """The model to call: the request's own, or the one this provider was built for."""
    name = request.model or fallback
    if not name:
        msg = (
            "no model was named: pass model= when building the provider, or set "
            "Request.model. A model id is the half after the colon in 'provider:model'."
        )
        raise ModelCallFailedError(msg)
    return name


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """How long the provider asked us to wait, or nothing at all.

    RFC 9110 allows both a count of seconds and an HTTP date, and providers send both.
    Anything else is treated as absent, because a header we cannot read is not a licence
    to make one up. `headers` is expected to be case-insensitive, as httpx's are.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    text = raw.strip()
    try:
        seconds = float(text)
    except ValueError:
        return _seconds_until(text)
    return max(0.0, seconds)


def _seconds_until(http_date: str) -> float | None:
    try:
        when = parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def error_message(body: str, *, fallback: str) -> str:
    """The provider's own sentence about what went wrong, if it sent one.

    Falls back to the body itself, condensed onto one line, because a proxy's plain-text
    "Bad Gateway" says more about a failure than a generic sentence we wrote would.
    """
    payload = json_object(body)
    error = payload.get("error") if payload else None
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str) and message.strip():
        return message.strip()
    condensed = " ".join(body.split())[:MESSAGE_LIMIT]
    return condensed or fallback


def check_status(*, provider: str, status: int, headers: Mapping[str, str], body: str) -> None:
    """Turn a wire status into the one exception the loop should see, or return.

    429 and 5xx are the retryable pair, and they carry whatever interval the provider
    asked for. Every other 4xx is a fact about the request rather than about the weather,
    and comes back as a failure the loop must not repeat.
    """
    if status < HTTPStatus.BAD_REQUEST:
        return
    detail = error_message(body, fallback="no detail given")
    message = f"{provider} answered {status}: {detail}"
    if status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        raise ModelUnavailableError(message, retry_after=retry_after_seconds(headers))
    raise ModelCallFailedError(message)


def sse_event(line: str) -> dict[str, Any] | None:
    """The JSON object on one `data:` line.

    Nothing for the framing around it: the `event:` lines, the blank separators, the
    keep-alive comments, and the `[DONE]` sentinel, which is a word rather than JSON.
    """
    if not line.startswith(DATA_PREFIX):
        return None
    return json_object(line[len(DATA_PREFIX) :].strip())


async def send(
    client: httpx.AsyncClient, request: httpx.Request, *, provider: str
) -> dict[str, Any]:
    """One non-streaming call, as a parsed object or as the failure it turned out to be."""
    try:
        response = await client.send(request)
    except httpx.HTTPError as exc:
        msg = f"{provider} could not be reached: {exc}"
        raise ModelUnavailableError(msg) from exc
    check_status(
        provider=provider,
        status=response.status_code,
        headers=response.headers,
        body=response.text,
    )
    payload = json_object(response.text)
    if payload is None:
        msg = f"{provider} answered {response.status_code} with something that is not JSON"
        raise ModelCallFailedError(msg)
    return payload


async def events(
    client: httpx.AsyncClient, request: httpx.Request, *, provider: str
) -> AsyncIterator[dict[str, Any]]:
    """Every server-sent event of one streaming call, already parsed.

    A failed stream is read far enough to fail loudly before the first event: a stream
    that began with a 429 must raise what the non-streaming path would raise, not yield
    zero chunks and let the caller conclude the model had nothing to say.
    """
    try:
        response = await client.send(request, stream=True)
        try:
            if response.status_code >= HTTPStatus.BAD_REQUEST:
                await response.aread()
                check_status(
                    provider=provider,
                    status=response.status_code,
                    headers=response.headers,
                    body=response.text,
                )
            async for line in response.aiter_lines():
                event = sse_event(line)
                if event is not None:
                    yield event
        finally:
            await response.aclose()
    except httpx.HTTPError as exc:
        msg = f"{provider} could not be reached: {exc}"
        raise ModelUnavailableError(msg) from exc


__all__ = [
    "CHUNK_DONE",
    "CHUNK_REASONING",
    "CHUNK_TEXT",
    "ModelCallFailedError",
    "as_dict",
    "as_list",
    "as_text",
    "check_status",
    "count",
    "error_message",
    "events",
    "json_object",
    "model_for",
    "retry_after_seconds",
    "send",
    "sse_event",
]
