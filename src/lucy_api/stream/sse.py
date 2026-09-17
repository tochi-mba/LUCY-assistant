"""Server-sent events for `GET /v1/sessions/{id}/events`, and how a client comes back.

SSE rather than a websocket, for a stream that is one-directional by design. The write path
is `POST /v1/sessions/{id}/inputs` and nothing a client wants to say belongs on this
connection, so a duplex protocol would buy an upgrade handshake, a ping/pong, and a second
set of proxy problems in exchange for a channel nobody writes to. SSE is a `GET` that
survives every intermediary that understands HTTP, and browsers reconnect on their own.

## `starting_after` is the load-bearing one

Two cursors arrive at this route and they are not equals.

`?starting_after=<sequence_number>` is what the client chose to send, from the last event it
actually handled. `Last-Event-ID` is what the *browser* replays from the last `id:` it
happened to see, and between a client and this service there may be several proxies, any of
which can drop a header, buffer a frame the client never processed, or retry a request
without it. So the query parameter wins whenever both are present, and a client that cares
about not missing anything sends it.

Both are still validated rather than quietly ignored, because the whole promise of a resume
cursor is that what comes back is complete. A cursor that could not be understood is an
error naming the fix; silently treating it as a fresh connection would hand back a snapshot
and drop the deltas in between, which is the exact failure this design exists to prevent.

## The id on the wire is the sequence number

`Last-Event-ID` is only useful for resumption if it can be compared and ordered, so `id:`
carries `sequence_number` and the opaque `event_id` rides inside the body where an audit
trail can quote it. An `id:` of `evt_9RnQ...` would need a lookup to mean anything, and
would mean nothing at all once the row it names had been trimmed.

## Heartbeats carry no id

A quiet turn is indistinguishable from a dead connection to everything between here and the
client, and something in that chain will eventually reclaim the socket. So a heartbeat goes
out on every idle tick. It deliberately has no `id:` field: it is not in the log, it has no
sequence number of its own, and a frame that moved a browser's `Last-Event-ID` to a number
meaning nothing would turn the keep-alive into a way to lose events.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from lucy_api.core.errors import LucyError
from lucy_api.sessions.sql_store import encoded
from lucy_api.stream.emitter import transport_event
from lucy_api.stream.events import STREAM_DONE, STREAM_ERROR, STREAM_HEARTBEAT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from lucy_api.stream.emitter import Subscriber
    from lucy_api.stream.events import Event


MEDIA_TYPE = "text/event-stream"

RETRY_MILLISECONDS = 3000
"""What a browser waits before reconnecting on its own. Sent once, in the opening frame."""

HEARTBEAT_SECONDS = 15.0
"""Chosen under the shortest idle timeout in the usual chain, which is a minute."""

HEADERS = {
    "Content-Type": MEDIA_TYPE + "; charset=utf-8",
    # `no-transform` is the half that matters: a proxy that helpfully compresses or rewrites
    # a stream is a proxy that buffers it, and a buffered event stream is a broken one.
    "Cache-Control": "no-cache, no-transform",
    # nginx buffers proxied responses by default and this is the documented way off it.
    "X-Accel-Buffering": "no",
}
"""The response headers this encoding needs.

No `Connection: keep-alive`. It is the default in HTTP/1.1 and it is a forbidden
hop-by-hop header in HTTP/2, so sending it is either redundant or wrong.
"""

OPENING = f"retry: {RETRY_MILLISECONDS}\n: lucy\n\n"
"""Sets the browser's reconnect delay, and pushes bytes so a proxy commits to the stream."""

CURSOR_PROBLEM = "invalid-cursor"
"""The problem type a cursor that could not be understood is reported as."""

SLOW_CONSUMER = "slow_consumer"
"""Why a connection ended when the reader could not keep up with the turn."""


def resume_from(starting_after: str | None, last_event_id: str | None) -> int | None:
    """The sequence number to resume after, or `None` for a connection starting fresh.

    `starting_after` wins over `Last-Event-ID` whenever both arrive, because one of them was
    chosen by the client and the other was replayed by a browser through however many
    proxies.
    """
    if starting_after is not None:
        return _cursor(starting_after, "starting_after")
    if last_event_id is not None:
        return _cursor(last_event_id, "Last-Event-ID")
    return None


def _cursor(raw: str, where: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        message = (
            f"{where} must be a sequence number, which is a whole number taken from the "
            "sequence_number of the last event handled"
        )
        raise LucyError(CURSOR_PROBLEM, message, 400) from None
    if value < 0:
        message = f"{where} must not be negative; 0 asks for the session from its beginning"
        raise LucyError(CURSOR_PROBLEM, message, 400)
    return value


def encode(event: Event) -> str:
    """One event as an SSE frame.

    The body is on a single `data:` line because JSON escapes the only character that could
    have split it. Re-joining a multi-line payload is work every client would otherwise have
    to do correctly, and most of them would do it nearly correctly.
    """
    return (
        f"id: {event.sequence_number}\nevent: {event.type}\ndata: {encoded(event.envelope())}\n\n"
    )


def heartbeat(session_id: str) -> str:
    """A liveness frame. Deliberately carries no `id:` -- see this module's docstring."""
    body = {"type": STREAM_HEARTBEAT, "session_id": session_id, "created_at": time.time()}
    return f"event: {STREAM_HEARTBEAT}\ndata: {encoded(body)}\n\n"


def closing(subscriber: Subscriber) -> Event:
    """The last frame, which says whether this was an ending or an interruption.

    A reader that fell behind is told so in the words that fix it. The alternative -- a
    connection that simply stops -- is indistinguishable from a network failure, and a
    client that cannot tell the two apart reconnects from the wrong place or not at all.
    """
    if subscriber.overflowed:
        return transport_event(
            subscriber.session_id,
            STREAM_ERROR,
            subscriber.last_sequence,
            {
                "reason": SLOW_CONSUMER,
                "message": (
                    f"this connection fell more than {subscriber.capacity} events behind "
                    f"and was closed; reconnect with "
                    f"?starting_after={subscriber.last_sequence}"
                ),
                "starting_after": subscriber.last_sequence,
            },
        )
    return transport_event(
        subscriber.session_id,
        STREAM_DONE,
        subscriber.last_sequence,
        {"starting_after": subscriber.last_sequence},
    )


async def frames(
    subscriber: Subscriber, *, heartbeat_seconds: float = HEARTBEAT_SECONDS
) -> AsyncIterator[str]:
    """Encode one subscription as SSE, until it ends or the client goes away.

    The generator does not end when a turn does. A session outlives its turns, a client
    watching one wants to see the next, and a dropped connection is not a cancellation --
    so the only things that stop this are the subscription closing and the reader walking
    away, which arrives as cancellation at the `await` below.
    """
    yield OPENING
    while True:
        event = await subscriber.next_event(heartbeat_seconds)
        if event is not None:
            yield encode(event)
        elif subscriber.ended:
            yield encode(closing(subscriber))
            return
        else:
            yield heartbeat(subscriber.session_id)


__all__ = [
    "CURSOR_PROBLEM",
    "HEADERS",
    "HEARTBEAT_SECONDS",
    "MEDIA_TYPE",
    "OPENING",
    "RETRY_MILLISECONDS",
    "SLOW_CONSUMER",
    "closing",
    "encode",
    "frames",
    "heartbeat",
    "resume_from",
]
