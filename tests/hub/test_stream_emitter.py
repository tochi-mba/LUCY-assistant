"""The emitter's one promise: after a reconnect, nothing is missing and nothing is twice.

Everything else here is in service of that. The resumption tests do the whole round trip --
emit, read some, disconnect, emit more, come back with `starting_after` -- and assert on the
exact sequence numbers received, because a test that only checks the last one would pass on
a stream that duplicated the middle.

Two kinds of substitute appear below, on purpose. `FakeEventLog` satisfies the `EventLog`
Protocol and is used wherever the question is about fan-out, ordering or back-pressure,
where a database would only add latency to something being asserted event by event. The
`SqlEventLog` tests use a real SQLite file through the real worker, because what they claim
-- that the sequence number comes from the table, that a trace id survives a table with no
column for it, that a row written by `sql_store` can still be replayed -- is entirely about
SQL, and a fake would be testing itself.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.sessions.sql_store import SessionStore, identifier
from lucy_api.store.worker import SqlWorker
from lucy_api.stream import events as taxonomy
from lucy_api.stream.emitter import (
    EventEmitter,
    NewEvent,
    SqlEventLog,
    Subscriber,
    event_from_row,
)
from lucy_api.stream.events import Event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

ACCOUNT = "acct_stream"
SESSION = "ses_one"
OTHER = "ses_two"


class FakeEventLog:
    """An in-memory `EventLog`, numbering exactly as the table does."""

    def __init__(self) -> None:
        self.rows: dict[str, list[Event]] = {}

    async def append(self, session_id: str, event: NewEvent) -> Event:
        rows = self.rows.setdefault(session_id, [])
        stored = Event(
            type=event.type,
            sequence_number=len(rows) + 1,
            event_id=identifier("evt"),
            session_id=session_id,
            created_at=time.time(),
            data=dict(event.data),
            turn_id=event.turn_id,
            agent_id=event.agent_id,
            trace_id=event.trace_id,
        )
        rows.append(stored)
        return stored

    async def latest(self, session_id: str) -> int:
        return len(self.rows.get(session_id, ()))

    async def replay(self, session_id: str, *, after: int) -> tuple[Event, ...]:
        rows = self.rows.get(session_id, [])
        return tuple(row for row in rows if row.sequence_number > after)


class FakeSnapshotter:
    """A snapshot, plus a hook for staging the race a real snapshot read has."""

    def __init__(self, during: Callable[[], Awaitable[None]] | None = None) -> None:
        self.during = during
        self.calls = 0

    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        self.calls += 1
        if self.during is not None:
            await self.during()
        return {"session_id": session_id, "status": "in_progress"}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    """A real database; closing it matters because an open WAL pins `tmp_path`."""
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore, account: str = ACCOUNT) -> str:
    """A session row written straight through the schema, so `events` starts empty.

    Going through `SessionStore.create` would be shorter and would also write the session's
    own `lucy.session.created` event, which would make every sequence number in these tests
    one higher than the reader expects for a reason that has nothing to do with streaming.
    """
    session = identifier("ses")
    now = time.time()

    def apply(db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO sessions (id,account_id,profile,title,status,model,thinking_config,"
            "persona,harness_version,input_policy,durability_mode,permission_mode,incognito,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session,
                account,
                "personal",
                "Streaming",
                "idle",
                "openai:gpt-5",
                "default",
                "default",
                "0.1.0",
                "enqueue",
                "durable",
                "ask",
                0,
                now,
                now,
            ),
        )

    await store.transaction(apply)
    return session


def an_emitter(**kwargs: Any) -> tuple[EventEmitter, FakeEventLog, FakeSnapshotter]:
    log = FakeEventLog()
    snapshots = FakeSnapshotter(kwargs.pop("during", None))
    return EventEmitter(log, snapshots, **kwargs), log, snapshots


async def drain(subscriber: Subscriber) -> list[Event]:
    """Everything available right now. `0` means do not block, never "deliver nothing"."""
    received: list[Event] = []
    while True:
        event = await subscriber.next_event(0)
        if event is None:
            return received
        received.append(event)


def sequences(received: list[Event]) -> list[int]:
    return [event.sequence_number for event in received]


def deltas(received: list[Event]) -> list[Event]:
    """What was in the log, as opposed to the frames the connection invented."""
    return [event for event in received if event.type not in taxonomy.TRANSPORT_TYPES]


async def test_a_sequence_number_comes_from_the_table_and_only_ever_climbs(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    log = SqlEventLog(store)

    first = await log.append(session, NewEvent(taxonomy.TURN_STARTED, {"n": 1}))
    second = await log.append(session, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": 2}))

    assert (first.sequence_number, second.sequence_number) == (1, 2)
    assert await log.latest(session) == 2
    assert first.event_id != second.event_id
    assert sequences(list(await log.replay(session, after=0))) == [1, 2]
    assert sequences(list(await log.replay(session, after=2))) == []


async def test_a_malformed_event_type_is_refused_before_anything_is_written() -> None:
    """A typo in a type is invisible in the test that meant to write it and permanent."""
    emitter, log, _ = an_emitter()

    with pytest.raises(ValueError, match="not a well-formed event type") as caught:
        await emitter.emit(SESSION, NewEvent("lucy.turn"))

    assert "lucy.<domain>.<noun>.<verb>" in str(caught.value)
    assert log.rows == {}


async def test_an_unknown_but_well_formed_type_is_emitted_because_the_catalogue_is_open(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    emitter = EventEmitter(SqlEventLog(store), FakeSnapshotter())

    stored = await emitter.emit(session, NewEvent("lucy.acme.widget.polished"))

    assert stored.sequence_number == 1


async def test_a_client_joining_mid_run_is_given_the_state_and_then_only_what_follows() -> None:
    """The second client in a session must not be sent the first client's whole history."""
    emitter, _, snapshots = an_emitter()
    for index in range(3):
        await emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))

    async with emitter.subscribe(SESSION) as subscriber:
        opening = await drain(subscriber)
        await emitter.emit(SESSION, NewEvent(taxonomy.TURN_COMPLETED))
        following = await drain(subscriber)

    assert snapshots.calls == 1
    assert [event.type for event in opening] == [taxonomy.STREAM_SNAPSHOT]
    assert opening[0].data["state"]["status"] == "in_progress"
    assert opening[0].sequence_number == 3
    assert sequences(following) == [4]


async def test_a_reconnection_receives_exactly_what_it_missed_and_nothing_it_already_had() -> None:
    """The whole point of the design, tested the way it actually happens.

    Read three, lose the wire, three more land while nobody is listening, come back with
    `starting_after`. The assertion is on the full list of sequence numbers, because a
    stream that repeated the middle would still end in the right place.
    """
    emitter, _, _ = an_emitter()

    async with emitter.subscribe(SESSION) as first:
        for index in range(3):
            await emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))
        seen = await drain(first)

    assert sequences(deltas(seen)) == [1, 2, 3]
    assert first.last_sequence == 3

    for index in range(3, 6):
        await emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))

    async with emitter.subscribe(SESSION, starting_after=first.last_sequence) as second:
        resumed = await drain(second)

    assert [event.type for event in resumed[:2]] == [
        taxonomy.STREAM_SNAPSHOT,
        taxonomy.STREAM_RESUMED,
    ]
    assert resumed[1].data == {"starting_after": 3, "replayed": 3}
    assert sequences(deltas(resumed)) == [4, 5, 6]
    assert [event.data["n"] for event in deltas(resumed)] == [3, 4, 5]


async def test_reconnecting_from_the_very_end_replays_nothing() -> None:
    """The common case: the wifi came back before anything else happened."""
    emitter, _, _ = an_emitter()
    await emitter.emit(SESSION, NewEvent(taxonomy.TURN_STARTED))

    async with emitter.subscribe(SESSION, starting_after=1) as subscriber:
        received = await drain(subscriber)

    assert deltas(received) == []
    assert received[-1].data == {"starting_after": 1, "replayed": 0}


async def test_an_event_emitted_while_the_snapshot_was_being_built_arrives_exactly_once() -> None:
    """Registering before reading is what makes this a duplicate rather than a loss.

    The alternative ordering -- read the log, then start listening -- drops this event
    entirely, and does it in a window narrow enough that nobody would find it for a year.
    """
    emitter: EventEmitter | None = None

    async def emit_during_the_snapshot() -> None:
        assert emitter is not None
        await emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": "racing"}))

    emitter, _, _ = an_emitter(during=emit_during_the_snapshot)

    async with emitter.subscribe(SESSION, starting_after=0) as subscriber:
        received = await drain(subscriber)
        assert sequences(deltas(received)) == [1]

        await emitter.emit(SESSION, NewEvent(taxonomy.TURN_COMPLETED))
        assert sequences(await drain(subscriber)) == [2]


async def test_a_reader_that_falls_behind_is_dropped_rather_than_allowed_to_stall_a_turn() -> None:
    """Back-pressure here would let one client on bad wifi halt everybody else's run."""
    emitter, _, _ = an_emitter(capacity=2)

    async with emitter.subscribe(SESSION) as slow:
        assert [event.type for event in await drain(slow)] == [taxonomy.STREAM_SNAPSHOT]
        for index in range(5):
            await emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))

        assert slow.overflowed
        assert emitter.subscriber_count(SESSION) == 0

        received = await drain(slow)

    assert sequences(received) == [1, 2]
    assert slow.ended
    assert slow.last_sequence == 2
    assert await slow.next_event(0) is None


async def test_one_slow_reader_does_not_take_the_others_with_it() -> None:
    """The slow one is the inner subscription, so it is released while the other is live.

    That ordering is the point: a subscriber the fan-out already dropped must leave without
    disturbing the roster, and a roster that still has somebody in it must not be discarded.
    """
    emitter, _, _ = an_emitter(capacity=2)

    async with emitter.subscribe(SESSION) as attentive, emitter.subscribe(SESSION) as slow:
        await drain(slow)
        await drain(attentive)
        for index in range(5):
            await emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))
            await drain(attentive)

        assert slow.overflowed
        assert emitter.subscriber_count(SESSION) == 1
        assert not attentive.overflowed

    assert emitter.subscriber_count(SESSION) == 0


async def test_delivery_order_is_sequence_order_even_when_a_run_emits_all_at_once() -> None:
    """Two turns in one session emit concurrently; a client must not have to reorder."""
    emitter, _, _ = an_emitter(capacity=64)

    async with emitter.subscribe(SESSION) as subscriber:
        await drain(subscriber)
        await asyncio.gather(
            *(
                emitter.emit(SESSION, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))
                for index in range(20)
            )
        )
        received = await drain(subscriber)

    assert sequences(received) == list(range(1, 21))


async def test_an_event_reaches_only_the_session_it_belongs_to() -> None:
    emitter, _, _ = an_emitter()

    async with emitter.subscribe(SESSION) as mine, emitter.subscribe(OTHER) as theirs:
        await drain(mine)
        await drain(theirs)
        await emitter.emit(SESSION, NewEvent(taxonomy.TURN_STARTED))

        assert len(await drain(mine)) == 1
        assert await drain(theirs) == []


async def test_a_quiet_stream_hands_control_back_so_the_transport_can_say_something() -> None:
    emitter, _, _ = an_emitter()

    async with emitter.subscribe(SESSION) as subscriber:
        await drain(subscriber)
        started = time.perf_counter()

        assert await subscriber.next_event(0.02) is None

        assert time.perf_counter() - started >= 0.01
        assert not subscriber.ended


async def test_closing_a_subscription_twice_changes_nothing() -> None:
    """A context manager already closed it; a caller closing it again is not an error."""
    subscriber = Subscriber(SESSION, capacity=4)
    subscriber.close()
    subscriber.close()

    assert subscriber.closed
    assert not subscriber.deliver(_an_event(1))
    assert await subscriber.next_event(0) is None
    assert subscriber.ended


async def test_leaving_a_subscription_forgets_it() -> None:
    emitter, _, _ = an_emitter()

    async with emitter.subscribe(SESSION) as subscriber:
        assert emitter.subscriber_count(SESSION) == 1

    assert emitter.subscriber_count(SESSION) == 0
    assert subscriber.closed


async def test_emitting_into_a_session_nobody_is_watching_is_still_recorded() -> None:
    emitter, log, _ = an_emitter()

    stored = await emitter.emit(SESSION, NewEvent(taxonomy.MEMORY_WRITTEN, {"topic": "home"}))

    assert stored.sequence_number == 1
    assert log.rows[SESSION][0].data == {"topic": "home"}


async def test_a_trace_id_survives_a_table_that_has_no_column_for_one(
    store: SessionStore,
) -> None:
    """The compromise in `TRACE_KEY`, asserted so that it stays a compromise and not a leak."""
    session = await a_session(store)
    log = SqlEventLog(store)

    await log.append(
        session,
        NewEvent(
            taxonomy.SECURITY_TOKEN_MINTED,
            {"audience": "spotify-api"},
            turn_id="trn_1",
            agent_id="agt_1",
            trace_id="trace_9",
        ),
    )
    await log.append(session, NewEvent(taxonomy.TURN_COMPLETED))

    traced, plain = await log.replay(session, after=0)

    assert traced.trace_id == "trace_9"
    assert traced.turn_id == "trn_1"
    assert traced.agent_id == "agt_1"
    # The reserved key is lifted back out rather than left in the body for a client to
    # find under two different names.
    assert traced.data == {"audience": "spotify-api"}
    assert plain.trace_id is None
    assert plain.agent_id is None


async def test_a_row_the_session_store_wrote_can_still_be_replayed(
    store: SessionStore,
) -> None:
    """`sql_store.event_row` predates this module and may have written a bare scalar."""
    session = await a_session(store)
    await store.event(ACCOUNT, session, taxonomy.SESSION_UPDATED, {"title": "renamed"})
    await store.event(ACCOUNT, session, taxonomy.SESSION_UPDATED, None)

    replayed = await SqlEventLog(store).replay(session, after=0)

    assert [event.data for event in replayed] == [{"title": "renamed"}, {"value": None}]


def test_an_event_rebuilt_from_a_row_keeps_every_column_it_was_given() -> None:
    row = {
        "event_id": "evt_1",
        "session_id": SESSION,
        "sequence_number": 7,
        "type": taxonomy.TOOL_FINISHED,
        "turn_id": None,
        "agent_id": None,
        "data": {"tool_call_id": "call_1", "trace_id": "trace_1"},
        "created_at": 9.5,
    }

    rebuilt = event_from_row(row)

    assert rebuilt == Event(
        type=taxonomy.TOOL_FINISHED,
        sequence_number=7,
        event_id="evt_1",
        session_id=SESSION,
        created_at=9.5,
        data={"tool_call_id": "call_1"},
        trace_id="trace_1",
    )


def _an_event(sequence: int) -> Event:
    return Event(
        type=taxonomy.CONTENT_TEXT_DELTA,
        sequence_number=sequence,
        event_id=f"evt_{sequence}",
        session_id=SESSION,
        created_at=float(sequence),
    )
