"""The second encoding: the Vercel AI SDK UI Message Stream, offered by negotiation.

A browser client written against `useChat` already knows how to render a stream -- text
arriving in pieces, reasoning it can fold away, a tool call whose arguments fill in before
the result does. Handing it `lucy.*` events instead means somebody writes an adapter, and
then maintains it, and then discovers that their adapter and ours disagree about what a
half-arrived tool argument looks like.

So Lucy speaks their protocol when asked. `Accept: text/event-stream` **plus**
`x-lucy-ui-message-stream: v1` selects it; anything else gets the native stream. Negotiation
rather than a second route, because it is the same events over the same connection with the
same resumption behaviour -- only the vocabulary differs.

## The native stream stays the default

This one is a **projection**, and a lossy one. There is no UI part for a probe that failed,
a memory that was written, a lease that expired or a sub-agent that was reaped, and inventing
`data-*` parts for all hundred and sixty-nine would be forking the protocol while claiming
to speak it. Events with no counterpart are omitted. A client that wants everything asks for
the native stream, which is what the CLI does.

The one Lucy-specific part that *is* carried is `data-lucy-connection-required`, because the
alternative is a tool that silently does nothing when a person has not connected Spotify.
`data-*` is the AI SDK's own extension point and a client that ignores it still works.

## Two response headers, deliberately

`x-vercel-ai-ui-message-stream: v1` is what the SDK's own reader looks for, so it has to be
the one on the wire. `x-lucy-ui-message-stream: v1` is echoed beside it so that the request
header a client sent and the response header it gets back have the same name, which is what
somebody reading a network tab is looking for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.sessions.sql_store import encoded
from lucy_api.stream import events as taxonomy
from lucy_api.stream.sse import HEADERS, HEARTBEAT_SECONDS, MEDIA_TYPE, closing

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from lucy_api.stream.emitter import Subscriber
    from lucy_api.stream.events import Event


NEGOTIATION_HEADER = "x-lucy-ui-message-stream"
"""The request header that asks for this encoding."""

VERCEL_HEADER = "x-vercel-ai-ui-message-stream"
"""The response header the SDK's reader checks for. Not ours to rename."""

PROTOCOL_VERSION = "v1"

RESPONSE_HEADERS = {
    **HEADERS,
    VERCEL_HEADER: PROTOCOL_VERSION,
    NEGOTIATION_HEADER: PROTOCOL_VERSION,
}

DONE = "data: [DONE]\n\n"
"""The sentinel the SDK's reader stops on. Not JSON, and deliberately not an event."""

PING = ": ping\n\n"
"""The keep-alive for this encoding: an SSE comment, invisible to any parser.

The native stream sends a `lucy.stream.heartbeat` event because its readers are ours and
can be told what one is. This reader is not ours, and a part type its version has never
heard of is a risk taken for nothing.
"""

CONNECTION_REQUIRED_PART = "data-lucy-connection-required"


def negotiated(accept: str | None, requested: str | None) -> bool:
    """Whether this request asked for the UI Message Stream. Both signals are required."""
    if accept is None or MEDIA_TYPE not in accept:
        return False
    return requested is not None and requested.strip().lower() == PROTOCOL_VERSION


def encode(part: Mapping[str, Any]) -> str:
    """One UI message part as an SSE data frame."""
    return f"data: {encoded(dict(part))}\n\n"


def _block_id(event: Event) -> str:
    """Which text or reasoning block a delta belongs to.

    A block that was never given an id is the turn's only one, so the turn's id serves. The
    session's id is the last resort, for an event that arrived outside any turn at all.
    """
    return str(event.data.get("id") or event.turn_id or event.session_id)


def _call_id(event: Event) -> str | None:
    """The tool call a part attaches to, or `None` when the event does not name one."""
    supplied = event.data.get("tool_call_id")
    if supplied is None:
        return None
    return str(supplied)


def _text(data: Mapping[str, Any], key: str) -> str:
    return str(data.get(key, ""))


def _start(event: Event) -> tuple[dict[str, Any], ...]:
    part: dict[str, Any] = {"type": "start"}
    if event.turn_id is not None:
        # The SDK keys a message by this, so a turn is a message and the ids line up with
        # what `GET /v1/turns/{id}` returns for the same thing.
        part["messageId"] = event.turn_id
    return (part,)


def _text_start(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "text-start", "id": _block_id(event)},)


def _text_delta(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "text-delta", "id": _block_id(event), "delta": _text(event.data, "delta")},)


def _text_end(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "text-end", "id": _block_id(event)},)


def _reasoning_start(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "reasoning-start", "id": _block_id(event)},)


def _reasoning_delta(event: Event) -> tuple[dict[str, Any], ...]:
    return (
        {"type": "reasoning-delta", "id": _block_id(event), "delta": _text(event.data, "delta")},
    )


def _reasoning_end(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "reasoning-end", "id": _block_id(event)},)


def _tool_input_start(event: Event) -> tuple[dict[str, Any], ...]:
    call = _call_id(event)
    if call is None:
        return ()
    return (
        {
            "type": "tool-input-start",
            "toolCallId": call,
            "toolName": _text(event.data, "tool_name"),
        },
    )


def _tool_input_delta(event: Event) -> tuple[dict[str, Any], ...]:
    call = _call_id(event)
    if call is None:
        return ()
    return (
        {
            "type": "tool-input-delta",
            "toolCallId": call,
            "inputTextDelta": _text(event.data, "delta"),
        },
    )


def _tool_input_available(event: Event) -> tuple[dict[str, Any], ...]:
    call = _call_id(event)
    if call is None:
        return ()
    return (
        {
            "type": "tool-input-available",
            "toolCallId": call,
            "toolName": _text(event.data, "tool_name"),
            "input": event.data.get("input"),
        },
    )


def _tool_output(event: Event) -> tuple[dict[str, Any], ...]:
    call = _call_id(event)
    if call is None:
        return ()
    return (
        {"type": "tool-output-available", "toolCallId": call, "output": event.data.get("output")},
    )


def _tool_error(event: Event) -> tuple[dict[str, Any], ...]:
    """A failed tool. Not in the plan's list, and the stream is wrong without it.

    A client that is shown `tool-input-available` and then nothing leaves a spinner turning
    for the rest of the conversation, which is a worse way to report a failure than any
    wording could be.
    """
    call = _call_id(event)
    if call is None:
        return ()
    return (
        {
            "type": "tool-output-error",
            "toolCallId": call,
            "errorText": _text(event.data, "message"),
        },
    )


def _approval(event: Event) -> tuple[dict[str, Any], ...]:
    part: dict[str, Any] = {
        "type": "tool-approval-request",
        "approvalId": str(event.data.get("approval_id") or event.event_id),
    }
    call = _call_id(event)
    if call is not None:
        part["toolCallId"] = call
    return (part,)


def _connection_required(event: Event) -> tuple[dict[str, Any], ...]:
    # `id` lets the SDK reconcile repeats: asking twice for Spotify replaces the first
    # prompt rather than stacking a second one under it.
    return (
        {
            "type": CONNECTION_REQUIRED_PART,
            "id": str(event.data.get("service") or event.event_id),
            "data": dict(event.data),
        },
    )


def _finish(_event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "finish"},)


def _failed(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "error", "errorText": _text(event.data, "message")}, {"type": "finish"})


def _error(event: Event) -> tuple[dict[str, Any], ...]:
    return ({"type": "error", "errorText": _text(event.data, "message")},)


CONVERTERS: Mapping[str, Callable[[Event], tuple[dict[str, Any], ...]]] = {
    taxonomy.TURN_STARTED: _start,
    taxonomy.CONTENT_TEXT_START: _text_start,
    taxonomy.CONTENT_TEXT_DELTA: _text_delta,
    taxonomy.CONTENT_TEXT_END: _text_end,
    taxonomy.CONTENT_REASONING_START: _reasoning_start,
    taxonomy.CONTENT_REASONING_DELTA: _reasoning_delta,
    taxonomy.CONTENT_REASONING_END: _reasoning_end,
    taxonomy.TOOL_INPUT_START: _tool_input_start,
    taxonomy.TOOL_INPUT_DELTA: _tool_input_delta,
    taxonomy.TOOL_INPUT_AVAILABLE: _tool_input_available,
    taxonomy.TOOL_FINISHED: _tool_output,
    taxonomy.TOOL_FAILED: _tool_error,
    taxonomy.APPROVAL_REQUESTED: _approval,
    taxonomy.CONNECTION_REQUIRED: _connection_required,
    taxonomy.TURN_COMPLETED: _finish,
    taxonomy.TURN_CANCELLED: _finish,
    taxonomy.TURN_FAILED: _failed,
    taxonomy.STREAM_ERROR: _error,
}
"""The whole projection, in one table. Everything absent from it is deliberately dropped."""


def parts(event: Event) -> tuple[dict[str, Any], ...]:
    """The UI message parts one Lucy event becomes. Empty when it has no counterpart."""
    convert = CONVERTERS.get(event.type)
    if convert is None:
        return ()
    return convert(event)


async def frames(
    subscriber: Subscriber, *, heartbeat_seconds: float = HEARTBEAT_SECONDS
) -> AsyncIterator[str]:
    """Encode one subscription as a UI Message Stream, ending with the SDK's sentinel."""
    while True:
        event = await subscriber.next_event(heartbeat_seconds)
        if event is not None:
            for part in parts(event):
                yield encode(part)
        elif subscriber.ended:
            for part in parts(closing(subscriber)):
                yield encode(part)
            yield DONE
            return
        else:
            yield PING


__all__ = [
    "CONNECTION_REQUIRED_PART",
    "CONVERTERS",
    "DONE",
    "NEGOTIATION_HEADER",
    "PING",
    "PROTOCOL_VERSION",
    "RESPONSE_HEADERS",
    "VERCEL_HEADER",
    "encode",
    "frames",
    "negotiated",
    "parts",
]
