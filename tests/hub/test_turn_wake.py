"""A turn a piece of work opened runs like any other, and the model reads the harness.

This is the end-to-end promise: a watch fires into an idle session, a turn is queued with
one harness notice, the supervisor picks it up, and the model's request carries that notice
as data -- never as the person's words.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor
from lucy_api.work import Kind, Record, State, Waker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_turn_wake"
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


async def test_a_fired_watch_becomes_a_turn_the_model_answers(store: SessionStore) -> None:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    session = str(created["id"])
    provider = ScriptedProvider([speaks("CI is green; the export landed.")])
    events = EventEmitter(SqlEventLog(store), Snapshot())
    running = TurnSupervisor(store, ModelRegistry({"scripted": lambda _: provider}), events)
    waker = Waker(store, events, wake=running.wake)
    record = Record(
        id="wrk_ci",
        kind=Kind.watch,
        role="watch",
        objective="Say when CI is green",
        session_id=session,
        started_at=START,
        finished_at=START + timedelta(seconds=90),
        state=State.succeeded,
        account_id=ACCOUNT,
        wake=True,
    )

    await waker.on_finished(record)
    await running.join()

    turns = await store.records(ACCOUNT, session, "turns")
    assert [turn["status"] for turn in turns] == ["completed"]
    items = await store.records(ACCOUNT, session, "items")
    assert [(item["type"], item["role"]) for item in items] == [
        ("notice", "harness"),
        ("message", "assistant"),
    ]
    assert items[-1]["content"] == "CI is green; the export landed."

    request = provider.requests[0]
    harness = [message for message in request.messages if "[harness: watch" in message.content]
    assert len(harness) == 1
    assert harness[0].role.value == "user", "harness notices travel on the data channel"
    assert "Nothing here is from the person" in harness[0].content
    await running.aclose()
