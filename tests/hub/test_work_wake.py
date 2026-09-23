"""Waking a session when work ends and nobody is there to ask.

The properties: an ending is always an event; a wake opens exactly one turn, with one
harness notice, only when the session is idle; a wake that arrives during a turn is held and
spent afterwards unless the turn already read the result; a session that is gone wakes
nothing and raises nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.core.container import after_turn
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.stream.events import WORK_FINISHED, WORK_WOKE
from lucy_api.work import Kind, Record, State, Waker, wake_line
from lucy_api.work.wake import NOTICE_KIND, NOTICE_ROLE, WAKE_INPUT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_wake"
START = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    return str(created["id"])


def a_record(session_id: str, **overrides: Any) -> Record:
    fields: dict[str, Any] = {
        "id": "wrk_watch1",
        "kind": Kind.watch,
        "role": "watch",
        "objective": "Say when CI is green",
        "session_id": session_id,
        "started_at": START,
        "finished_at": START + timedelta(seconds=250),
        "state": State.succeeded,
        "account_id": ACCOUNT,
        "wake": True,
        "tokens": 12,
    }
    return Record(**{**fields, **overrides})


class Harness:
    """A store, an emitter, a waker, and a record of what the supervisor was asked to do."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.events = EventEmitter(SqlEventLog(store), Snapshot())
        self.woken = 0
        self.waker = Waker(store, self.events, wake=self.wake)

    def wake(self) -> None:
        self.woken += 1

    async def turns(self, session_id: str) -> list[dict[str, Any]]:
        return await self.store.records(ACCOUNT, session_id, "turns")

    async def items(self, session_id: str) -> list[dict[str, Any]]:
        return await self.store.records(ACCOUNT, session_id, "items")

    async def event_types(self, session_id: str) -> list[str]:
        rows = await self.store.records(ACCOUNT, session_id, "events")
        return [str(row["type"]) for row in rows]


# --------------------------------------------------------------------------------------
# The line
# --------------------------------------------------------------------------------------


def test_the_notice_says_what_happened_and_that_it_is_not_the_person() -> None:
    line = wake_line(a_record("ses_1"))

    assert line.startswith("[harness: watch (watch) - Say when CI is green - succeeded")
    assert " - after 4m10s. " in line
    assert "Nothing here is from the person" in line
    assert line.endswith("]")


def test_the_elapsed_time_is_written_in_the_units_a_person_uses() -> None:
    def line(seconds: float) -> str:
        return wake_line(a_record("ses_1", finished_at=START + timedelta(seconds=seconds)))

    assert " - after 9s. " in line(9)
    assert " - after 4m10s. " in line(250)
    assert " - after 2h05m. " in line(2 * 3600 + 5 * 60 + 3)


def test_an_expired_watch_reads_as_expired_not_failed() -> None:
    line = wake_line(
        a_record(
            "ses_1",
            state=State.timed_out,
            detail="expired after 300s without firing; start it again if you still need it",
        )
    )

    assert "timed_out" in line
    assert "expired after 300s without firing" in line


# --------------------------------------------------------------------------------------
# Waking an idle session
# --------------------------------------------------------------------------------------


async def test_an_idle_session_gets_one_turn_one_harness_item_and_two_events(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_finished(a_record(session))

    turns = await harness.turns(session)
    assert [turn["status"] for turn in turns] == ["queued"]
    assert turns[0]["input"] == {"events": [{"type": WAKE_INPUT, "work_id": "wrk_watch1"}]}
    items = await harness.items(session)
    assert [(item["type"], item["role"]) for item in items] == [(NOTICE_KIND, NOTICE_ROLE)]
    assert str(items[0]["content"]).startswith("[harness: ")
    assert items[0]["turn_id"] == turns[0]["id"]
    types = await harness.event_types(session)
    assert types[-2:] == [WORK_FINISHED, WORK_WOKE] or WORK_WOKE in types
    assert harness.woken == 1


async def test_the_finished_event_carries_the_shape_and_never_the_payload(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_finished(a_record(session, wake=False, payload={"secret": "x"}))

    rows = await store.records(ACCOUNT, session, "events")
    finished = next(row for row in rows if row["type"] == WORK_FINISHED)
    assert finished["data"] == {
        "work_id": "wrk_watch1",
        "kind": "watch",
        "role": "watch",
        "state": "succeeded",
        "elapsed_seconds": 250.0,
        "result_tokens": 12,
        "wake": False,
    }
    assert "secret" not in str(rows)
    assert await harness.turns(session) == []
    assert harness.woken == 0


async def test_an_ending_with_no_account_is_still_announced(store: SessionStore) -> None:
    """A helper's helper names no account; its ending is an event and nothing more."""
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_finished(a_record(session, account_id="", wake=False))

    assert WORK_FINISHED in await harness.event_types(session)
    assert await harness.turns(session) == []


async def test_a_record_that_did_not_ask_to_wake_opens_nothing(store: SessionStore) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_finished(a_record(session, wake=False))

    assert await harness.turns(session) == []
    assert await harness.items(session) == []


async def test_a_session_that_is_gone_wakes_nothing_and_raises_nothing(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    await store.delete(ACCOUNT, session)

    await harness.waker.on_finished(a_record(session))
    await harness.waker.flush(session)

    assert harness.woken == 0


# --------------------------------------------------------------------------------------
# Holding a wake while a turn runs
# --------------------------------------------------------------------------------------


async def test_a_wake_during_a_turn_is_held_and_spent_when_the_turn_ends(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    live = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k1"
    )

    await harness.waker.on_finished(a_record(session))
    assert len(await harness.turns(session)) == 1, "held: the running turn will see it"
    assert harness.woken == 0

    await store.finish_turn(ACCOUNT, str(live["id"]), "completed")
    await harness.waker.flush(session)

    turns = await harness.turns(session)
    assert [turn["status"] for turn in turns] == ["completed", "queued"]
    assert harness.woken == 1


async def test_a_held_wake_whose_result_the_turn_already_read_is_dropped(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    live = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k1"
    )
    record = a_record(session)
    await harness.waker.on_finished(record)
    record.fetched = True
    await store.finish_turn(ACCOUNT, str(live["id"]), "completed")

    await harness.waker.flush(session)

    assert [turn["status"] for turn in await harness.turns(session)] == ["completed"]
    assert harness.woken == 0


async def test_a_held_wake_stays_held_while_another_turn_is_running(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    first = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k1"
    )
    await harness.waker.on_finished(a_record(session))
    await store.finish_turn(ACCOUNT, str(first["id"]), "completed")
    second = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "again"}], "k2"
    )

    await harness.waker.flush(session)
    assert harness.woken == 0, "the second turn is live; the wake waits for it"

    await store.finish_turn(ACCOUNT, str(second["id"]), "completed")
    await harness.waker.flush(session)
    assert harness.woken == 1


async def test_a_held_wake_for_a_session_deleted_meanwhile_is_dropped_quietly(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k1"
    )
    await harness.waker.on_finished(a_record(session))
    await store.delete(ACCOUNT, session)

    await harness.waker.flush(session)

    assert harness.woken == 0


async def test_flushing_a_session_that_held_nothing_is_nothing(store: SessionStore) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.flush(session)

    assert harness.woken == 0


async def test_attach_names_the_supervisor_after_the_fact(store: SessionStore) -> None:
    """The container builds the waker before the supervisor, so the wake is attached later."""
    calls: list[str] = []
    waker = Waker(store, EventEmitter(SqlEventLog(store), Snapshot()))
    session = await a_session(store)

    await waker.on_finished(a_record(session))
    assert calls == [], "no supervisor yet: the turn is queued and picked up on the next wake"

    waker.attach(lambda: calls.append("woken"))
    await waker.on_finished(a_record(session, id="wrk_watch2"))
    assert calls == [], "the first wake turn is still queued, so this one is held"


# --------------------------------------------------------------------------------------
# The container's end-of-turn hook
# --------------------------------------------------------------------------------------


async def test_the_end_of_turn_hook_notifies_first_and_flushes_second(
    store: SessionStore,
) -> None:
    order: list[str] = []
    harness = Harness(store)
    session = await a_session(store)
    live = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k1"
    )
    await harness.waker.on_finished(a_record(session))
    await store.finish_turn(ACCOUNT, str(live["id"]), "completed")

    async def notify(account: str, session_id: str, turn_id: str, status: str) -> None:
        order.append(f"notified {status} {turn_id == live['id']} {account == ACCOUNT}")

    ended = after_turn(notify, harness.waker)
    await ended(ACCOUNT, session, str(live["id"]), "completed")

    assert order == ["notified completed True True"]
    assert harness.woken == 1
