"""One append, two destinations: the durable log, and whoever happens to be watching.

A long turn will outlive a laptop's wifi. That single sentence decides the shape of
everything below. If the stream were only a fan-out of in-memory messages, a person who
walked into a lift halfway through a twelve-minute run would come back to a conversation
with a hole in it, and no amount of buffering would fix it -- a buffer large enough to
survive a commute is a buffer large enough to exhaust the process.

So the event log is the record and the fan-out is a convenience. Every event is written to
`events` inside a transaction first, and only then handed to live subscribers. Nothing a
subscriber receives is unrecoverable, because it is all still in SQLite, addressed by a
number that only climbs.

## The sequence number is assigned by the database, not by the process

`MAX(sequence_number) + 1` is read and written inside one `BEGIN IMMEDIATE`, on the single
thread that owns the connection, with `UNIQUE(session_id, sequence_number)` underneath. A
counter in Python would be a second source of truth: it would restart at one when the
process did, and it would disagree with the table the moment two workers existed.

## Delivery order is the sequence order, and that needs a lock

Appending is `await`, so two turns emitting at once could interleave: A takes 7, B takes 8,
and B's fan-out runs first because its future happened to resolve first. A client reading a
stream where 8 precedes 7 has to buffer and reorder, which is work nobody should be made to
do for events that were generated in order. One lock around append-and-fan-out costs
nothing -- the writes were already serialized by the worker thread -- and removes the
question.

## A slow reader is disconnected, never waited for

Each subscriber has a bounded queue. When it fills, the subscriber is closed and dropped:
its queue is not drained, the emitter never blocks, and the turn carries on for everybody
else. The alternative -- back-pressure -- would let one client on hotel wifi stall a run
that three other clients and a sub-agent are watching.

Being dropped is survivable precisely because of the log. The subscriber records the last
sequence number it actually handed over, the transport tells the client what it was, and
the client reconnects with `starting_after=<that number>` and is made whole. That is the
same path a reconnection after a lost network takes, so it is the same code and it is
exercised by the same tests.

## Joining late, and joining twice

`subscribe` registers the queue *before* it reads anything. An event emitted while the
snapshot is being built therefore lands in the queue, and would be delivered twice -- once
in the backlog and once live -- so the subscriber carries a floor and drops anything at or
below the highest sequence number its prologue already covered. Registering after the read
instead would lose that event entirely, which is the worse failure and the harder one to
notice.

## Authorization happened at the door

Nothing here takes an account. The route that opens a stream has already proved the caller
owns the session, and repeating the check in the fan-out would suggest that an unauthorized
caller could reach this far.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.sessions.sql_store import encoded, identifier, row_value
from lucy_api.stream.events import (
    STREAM_RESUMED,
    STREAM_SNAPSHOT,
    Event,
    is_well_formed,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncGenerator, Mapping

    from lucy_api.sessions.sql_store import SessionStore


DEFAULT_CAPACITY = 512
"""Events one subscriber may fall behind by. Roughly a minute of a busy turn."""

TRACE_KEY = "trace_id"
"""Where a trace id rides in the stored payload, because the table has no column for it.

`events` is a pinned contract and adding a column is not this module's to do, so the id is
written into the payload under a reserved key and lifted back out on the way in. It is a
compromise and it is written down here rather than discovered later by somebody wondering
why `data.trace_id` appears in one row and not the next.
"""


@dataclass(frozen=True, slots=True)
class NewEvent:
    """One event on its way in, before the database has given it a number.

    A row is a thing rather than five positional arguments. `data` is a mapping and not an
    arbitrary object: an event body is read by clients written in other languages, and a
    bare string or a list at the top level is a shape none of them can extend later.
    """

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    turn_id: str | None = None
    agent_id: str | None = None
    trace_id: str | None = None


class EventLog(Protocol):
    """Where events are kept, so that losing a connection is not losing anything."""

    async def append(self, session_id: str, event: NewEvent) -> Event: ...

    async def latest(self, session_id: str) -> int: ...

    async def replay(self, session_id: str, *, after: int) -> tuple[Event, ...]: ...


class Snapshotter(Protocol):
    """Whatever can describe a session's whole state in one object.

    Kept at arm's length on purpose. What belongs in a snapshot -- the session row, the
    running turn, pending approvals, the capability set -- is decided by the parts that own
    those things, and a stream that knew how to assemble it would have to import all of
    them.
    """

    async def snapshot(self, session_id: str) -> Mapping[str, Any]: ...


def transport_event(session_id: str, kind: str, position: int, data: dict[str, Any]) -> Event:
    """A frame a connection invented for itself. It is never written to a session's log.

    It still carries a sequence number, because the number is what a client resumes from
    and a frame without one would leave a client that disconnected on it with nothing to
    say. The number it carries is the client's own position, never a new one.
    """
    return Event(
        type=kind,
        sequence_number=position,
        event_id=identifier("evt"),
        session_id=session_id,
        created_at=time.time(),
        data=data,
    )


class Subscriber:
    """One live connection's view of a session, and the bounded queue behind it.

    `next_event` returning `None` means one of two different things, and the caller tells
    them apart with `ended`: either the wait elapsed and the transport should write a
    heartbeat, or the subscription is over and the transport should close. Collapsing them
    into an exception would make the heartbeat -- which happens constantly and is not an
    error -- cost a raise and a catch on every quiet second.
    """

    def __init__(self, session_id: str, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self.session_id = session_id
        self.capacity = capacity
        self.overflowed = False
        """Set when this subscriber was dropped for falling behind, not for disconnecting."""

        self.ended = False
        """Set once the end of the queue has actually been reached by a reader."""

        self.last_sequence = 0
        """The highest sequence number handed over. What the client resumes from."""

        # One slot beyond the advertised capacity is reserved so that closing a full queue
        # always has somewhere to put its sentinel. Without it, the one case that most
        # needs to be told it is over -- the reader that fell behind -- is the one case
        # that cannot be.
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=capacity + 1)
        self._prologue: deque[Event] = deque()
        self._floor = 0
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether anything further will ever be queued for this subscriber."""
        return self._closed

    def prologue(self, events: tuple[Event, ...], *, floor: int) -> None:
        """Install what this connection is owed before it starts following along."""
        self._prologue.extend(events)
        self._floor = max(self._floor, floor)

    def deliver(self, event: Event) -> bool:
        """Queue one event. False means this subscriber is gone and should be forgotten."""
        if self._closed:
            return False
        # A store mutation may write several events before its owner tells the emitter to
        # fan out.  A newly built emitter (after restart) deliberately starts with no
        # process-local high-water mark and replays that first batch from SQLite; anything
        # this subscriber already received in its prologue must therefore be harmless.
        if event.sequence_number <= self._floor:
            return True
        if self._queue.qsize() >= self.capacity:
            self.overflowed = True
            self.close()
            return False
        self._queue.put_nowait(event)
        return True

    def close(self) -> None:
        """Stop the subscription. A reader blocked on it wakes up and sees `ended`."""
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)

    async def next_event(self, idle_seconds: float | None = None) -> Event | None:
        """The next event, `None` on a quiet tick, `None` with `ended` when it is over.

        `idle_seconds` is a wait rather than a deadline on the work, which is why it is
        not called a timeout: nothing is cancelled when it elapses and no event is lost.
        The caller asked to be given control back so that it can write a heartbeat.

        What is already queued is taken without waiting, which is more than an
        optimisation. `wait_for` with a wait of zero cancels the read before it can run, so
        going through it unconditionally would make `next_event(0)` mean "never deliver
        anything" instead of "do not block" -- and it would put a task on the event loop for
        every event in a stream whose whole job is to be cheap.
        """
        while True:
            if self._prologue:
                owed = self._prologue.popleft()
                self.last_sequence = max(self.last_sequence, owed.sequence_number)
                return owed
            if self.ended:
                return None
            if self._queue.empty():
                try:
                    event = await asyncio.wait_for(self._queue.get(), idle_seconds)
                except TimeoutError:
                    return None
            else:
                event = self._queue.get_nowait()
            if event is None:
                self.ended = True
                return None
            if event.sequence_number <= self._floor:
                # Emitted between this connection registering and its backlog being read,
                # so it has already gone out as part of the prologue.
                continue
            self._floor = event.sequence_number
            self.last_sequence = event.sequence_number
            return event


class EventEmitter:
    """Writes an event down, then tells whoever is listening."""

    def __init__(
        self,
        log: EventLog,
        snapshots: Snapshotter,
        *,
        capacity: int = DEFAULT_CAPACITY,
    ) -> None:
        self._log = log
        self._snapshots = snapshots
        self.capacity = capacity
        self._subscribers: dict[str, list[Subscriber]] = {}
        self._published: dict[str, int] = {}
        # One lock, not one per session: the writes it orders are already serialized by the
        # single thread that owns the connection, so a finer lock would buy nothing and
        # would leak an entry per session that ever existed.
        self._lock = asyncio.Lock()

    def subscriber_count(self, session_id: str) -> int:
        """How many live connections a session has. For readiness and for tests."""
        return len(self._subscribers.get(session_id, ()))

    async def emit(self, session_id: str, event: NewEvent) -> Event:
        """Append one event and fan it out. The stored event, numbered, comes back."""
        if not is_well_formed(event.type):
            message = (
                f"{event.type!r} is not a well-formed event type; the grammar is "
                "'lucy.<domain>.<noun>.<verb>', lower case, with '_' inside a segment"
            )
            raise ValueError(message)
        async with self._lock:
            stored = await self._log.append(session_id, event)
            self._published[session_id] = stored.sequence_number
            self._fan_out(session_id, stored)
        return stored

    async def publish_persisted(self, session_id: str) -> None:
        """Fan out rows a session transaction already committed.

        Session writes and event writes share one SQLite transaction.  Writing a second
        event here would duplicate the audit trail, so owners call this *after* committing
        to replay just the rows not yet handed to live subscribers.  The high-water mark is
        process-local by design: after a restart it begins at zero, replays the durable log
        once, and subscribers drop anything their prologue already covered.
        """
        async with self._lock:
            after = self._published.get(session_id, 0)
            stored = await self._log.replay(session_id, after=after)
            for event in stored:
                self._fan_out(session_id, event)
            if stored:
                self._published[session_id] = stored[-1].sequence_number

    def _fan_out(self, session_id: str, event: Event) -> None:
        live = self._subscribers.get(session_id)
        if live is None:
            return
        for subscriber in tuple(live):
            if not subscriber.deliver(event):
                live.remove(subscriber)
        if not live:
            del self._subscribers[session_id]

    @asynccontextmanager
    async def subscribe(
        self, session_id: str, *, starting_after: int | None = None
    ) -> AsyncGenerator[Subscriber, None]:
        """Follow a session, from `starting_after` if the client is coming back.

        The prologue is a snapshot and then whatever was missed. The snapshot frame claims
        the *client's* position rather than the log's tail, so a client that dies the
        moment after reading it resumes from where it actually was. Its contents are newer
        than that position, which is the safe direction to be wrong in: re-applying a delta
        the snapshot already reflects is a redraw, while skipping one is a wrong screen for
        the rest of the session.
        """
        subscriber = Subscriber(session_id, capacity=self.capacity)
        self._subscribers.setdefault(session_id, []).append(subscriber)
        try:
            owed, floor = await self._prologue(session_id, starting_after)
            subscriber.prologue(owed, floor=floor)
            yield subscriber
        finally:
            self._release(subscriber)

    async def _prologue(
        self, session_id: str, starting_after: int | None
    ) -> tuple[tuple[Event, ...], int]:
        # The snapshot is read before the tail so that anything landing between the two is
        # delivered as a delta rather than falling into the gap between them.
        state = await self._snapshots.snapshot(session_id)
        tail = await self._log.latest(session_id)
        position = tail if starting_after is None else starting_after
        owed = [
            transport_event(
                session_id,
                STREAM_SNAPSHOT,
                position,
                {"state": dict(state), "sequence_number": tail},
            )
        ]
        if starting_after is not None:
            missed = await self._log.replay(session_id, after=starting_after)
            owed.append(
                transport_event(
                    session_id,
                    STREAM_RESUMED,
                    position,
                    {"starting_after": starting_after, "replayed": len(missed)},
                )
            )
            owed.extend(missed)
            if missed:
                tail = max(tail, missed[-1].sequence_number)
        return tuple(owed), tail

    def _release(self, subscriber: Subscriber) -> None:
        subscriber.close()
        live = self._subscribers.get(subscriber.session_id)
        if live is None:
            # Already forgotten, which is what falling behind does to a subscriber.
            return
        if subscriber in live:
            live.remove(subscriber)
        if not live:
            del self._subscribers[subscriber.session_id]


class SqlEventLog:
    """The `events` table, which is the half of the stream that survives a reboot."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def append(self, session_id: str, event: NewEvent) -> Event:
        def apply(db: sqlite3.Connection) -> Event:
            sequence: int = db.execute(
                "SELECT COALESCE(MAX(sequence_number),0)+1 FROM events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            stored = Event(
                type=event.type,
                sequence_number=sequence,
                event_id=identifier("evt"),
                session_id=session_id,
                created_at=time.time(),
                data=dict(event.data),
                turn_id=event.turn_id,
                agent_id=event.agent_id,
                trace_id=event.trace_id,
            )
            payload = dict(stored.data)
            if stored.trace_id is not None:
                payload[TRACE_KEY] = stored.trace_id
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                (
                    stored.event_id,
                    session_id,
                    sequence,
                    stored.type,
                    stored.turn_id,
                    stored.agent_id,
                    encoded(payload),
                    stored.created_at,
                ),
            )
            return stored

        return await self._store.transaction(apply)

    async def latest(self, session_id: str) -> int:
        def read(db: sqlite3.Connection) -> int:
            found: int = db.execute(
                "SELECT COALESCE(MAX(sequence_number),0) FROM events WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            return found

        return await self._store.worker.call(read)

    async def replay(self, session_id: str, *, after: int) -> tuple[Event, ...]:
        """Everything after a cursor, in order.

        Unbounded on purpose. A cap here would be a truncation nobody could see, and the
        one promise a resumption cursor makes is that what comes back is complete. The
        bound is the session's own event count, which is a session-lifetime concern and is
        answered by trimming the log, not by lying to the client reading it.
        """

        def read(db: sqlite3.Connection) -> tuple[Event, ...]:
            rows = db.execute(
                "SELECT * FROM events WHERE session_id=? AND sequence_number>? "
                "ORDER BY sequence_number",
                (session_id, after),
            ).fetchall()
            return tuple(event_from_row(row_value(row)) for row in rows)

        return await self._store.worker.call(read)


def event_from_row(value: dict[str, Any]) -> Event:
    """Rebuild an event from a decoded row, including the trace id riding in its payload."""
    stored = value["data"]
    # Rows written before this module existed, or by `sql_store.event_row`, may hold
    # something other than an object. A live stream is the wrong place to discover that,
    # so a scalar is presented under a key rather than crashing the connection reading it.
    payload = dict(stored) if isinstance(stored, dict) else {"value": stored}
    trace: str | None = payload.pop(TRACE_KEY, None)
    return Event(
        type=value["type"],
        sequence_number=value["sequence_number"],
        event_id=value["event_id"],
        session_id=value["session_id"],
        created_at=value["created_at"],
        data=payload,
        turn_id=value["turn_id"],
        agent_id=value["agent_id"],
        trace_id=trace,
    )


__all__ = [
    "DEFAULT_CAPACITY",
    "TRACE_KEY",
    "EventEmitter",
    "EventLog",
    "NewEvent",
    "Snapshotter",
    "SqlEventLog",
    "Subscriber",
    "event_from_row",
    "transport_event",
]
