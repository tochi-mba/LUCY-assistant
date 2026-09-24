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
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx

from lucy_api.model.types import SAY, ModelUnavailableError

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


FENCED = re.compile(r"\A\s*```[a-zA-Z]*[ \t]*\r?\n(?P<body>.*?)\r?\n?[ \t]*```\s*\Z", re.DOTALL)
"""A whole message that is one Markdown code fence and nothing else.

Anchored at both ends on purpose. Pulling the first fenced block out of a message that also
has prose around it would read "here is what that config looks like: ```{...}```" as a plan
and run it. One fence, alone, is the model formatting its answer; a fence inside a sentence
is the model quoting something.
"""


def json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in `text`, or nothing.

    Nothing is a real answer here rather than an error: it is how a plan the model wrote
    badly reaches the repair path with its text still intact. But "the model wrapped it in a
    code fence" is not writing it badly, and it used to land here all the same -- this was a
    bare `json.loads`, so the first real model ever pointed at this hub replied

        ```json
        {"steps":[{"id":"caps","op":"capabilities.list","input":{}}]}
        ```

    and the turn ended as a success with that text shown to the person as the answer. A plan
    read as prose is the worst of both: the steps never run, and the person is handed wire
    format. Fences are how models emit JSON when nothing is forcing them not to, so reading
    one is part of reading JSON.
    """
    candidate = FENCED.match(text)
    try:
        value = json.loads(candidate.group("body") if candidate else text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def plan_object(text: str) -> dict[str, Any] | None:
    """The plan in `text`, including one the model put a sentence in front of.

    :func:`json_object` is strict on purpose: a message is JSON or it is prose, and reading
    JSON out of prose would let a sentence that merely *contains* an object be executed. But
    models narrate. This one repeatedly did:

        The step timed out, but the command kept running and finished. I'm fetching its
        output now.

        {"steps":[{"id":"pyresult","op":"work.result","input":{...}}]}

    Every word of that is a progress note and every character of the object is a plan, and
    reading the pair as prose ends the turn -- showing the person the wire format and never
    running the step the model had already decided on.

    Narrower than it looks. The object has to be the *last* thing in the message, it has to
    parse on its own, and it has to carry a `steps` array, which is what makes a plan a plan.
    A final answer that ends by quoting a configuration is not mistaken for one.
    """
    whole = json_object(text)
    if whole is not None:
        return whole
    trailing = _trailing_object(text)
    return trailing if trailing is not None and isinstance(trailing.get("steps"), list) else None


def said_and_planned(text: str, *, narrated: bool = False) -> tuple[str, dict[str, Any] | None]:
    """What a reply says to the person, and the plan it asks to run, from one message.

    The shapes, in the order they are tried: a message that holds no object is prose, and
    is what it says. An object with `say` and no `steps` is an answer in words -- the only
    kind a provider enforcing the plan schema lets a model give. An object with steps is a
    plan, and its `say`, if any, is said while it runs. An object with neither is passed on
    as the plan it claims to be, so the repair path can quote it back.

    `narrated` reads a plan the model put a sentence in front of; see :func:`plan_object`.
    """
    found = plan_object(text) if narrated else json_object(text)
    if found is None:
        return text, None
    said = found.get(SAY)
    words = said.strip() if isinstance(said, str) else ""
    if SAY in found and "steps" not in found:
        return words, None
    return words, {key: value for key, value in found.items() if key != SAY}


def _trailing_object(text: str) -> dict[str, Any] | None:
    """The JSON object a message ends with, or nothing.

    Scanned backwards from the last `}` to the `{` that balances it, so a message with more
    than one object yields the one the model finished on -- which is the one it meant.
    """
    end = text.rstrip().rfind("}")
    if end < 0:
        return None
    body = text.rstrip()[: end + 1]
    depth = 0
    for index in range(len(body) - 1, -1, -1):
        character = body[index]
        if character == "}":
            depth += 1
        elif character == "{":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(body[index:])
                except ValueError:
                    return None
                return value if isinstance(value, dict) else None
    return None


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
        # Bounded like the fallback. A provider's sentence is usually short, and the one
        # that is not is the one echoing the request back at us -- which is exactly what
        # must not reach a transcript through an error's text.
        return " ".join(message.split())[:MESSAGE_LIMIT]
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
        raise ModelUnavailableError(_unreachable(provider, exc)) from exc
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
        raise ModelUnavailableError(_unreachable(provider, exc)) from exc


def _unreachable(provider: str, exc: httpx.HTTPError) -> str:
    """A transport failure, named by its type and never by its text.

    `str(exc)` on an httpx error carries the request URL. For a provider that authenticates
    in the query string, the URL *is* the credential, and this sentence ends up in an
    outcome, a transcript and a log line. The type -- `ConnectTimeout`, `ReadTimeout`,
    `ConnectError` -- says everything a person needs to act on.
    """
    return f"{provider} could not be reached ({type(exc).__name__})"


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
    "said_and_planned",
    "send",
    "sse_event",
]
