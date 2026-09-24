"""A helper the hub was running when it restarted is announced to its conversation, once.

The roster survives a restart and the work registry does not. The supervisor already marked
such a helper `interrupted` on start, but nothing told the conversation that started it: the
notice it was promised never came, `agents.list` shows only what is running, and the next
turn's live block said nothing. The helper had simply vanished, and so had the chance to
continue it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.agents.restart import announce_interrupted
from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore, Interrupted
from lucy_api.agents.types import CONTINUABLE, RESTARTED
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.packs.agents import AgentsPack, _reopen, _spawn
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.packs.work import _check
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import NewItem, SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor
from lucy_api.work.registry import Registry
from lucy_api.work.types import Brief, Kind, State

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

ACCOUNT = "acct_restart"
STARTED = 1_790_000_000.0
"""When the interrupted helper began, fixed so how long it ran is a number, not a race."""


class _Snapshot:
    async def snapshot(self, session_id: str) -> dict[str, str]:
        return {"session_id": session_id}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def _left_running(
    store: SessionStore, *, depth: int = 1, worked_for: float | None = 240.0
) -> tuple[str, str]:
    """A conversation with a helper a previous process was running when it went down."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    session = str(created["id"])
    agent_id = await AgentStore(store).insert(
        ACCOUNT, session, role="reader", objective="Read every file", depth=depth
    )
    if worked_for is not None:
        await store.append(
            ACCOUNT, session, NewItem("message", "assistant", "halfway", agent_id=agent_id)
        )

    def backdate(db: sqlite3.Connection) -> None:
        db.execute("UPDATE agents SET created_at=? WHERE id=?", (STARTED, agent_id))
        if worked_for is not None:
            db.execute(
                "UPDATE items SET created_at=? WHERE agent_id=?", (STARTED + worked_for, agent_id)
            )

    await store.transaction(backdate)
    return session, agent_id


async def _restart(store: SessionStore, work: Registry | None) -> None:
    capabilities = Capabilities((HelpPack(), AgentsPack()), work=work)
    supervisor = TurnSupervisor(
        store,
        ModelRegistry({}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities,
        agents=AgentStore(store),
    )
    await supervisor.start()
    await supervisor.aclose()


def _registry() -> Registry:
    return Registry(now=lambda: datetime.now(UTC))


async def test_a_helper_the_restart_stopped_is_the_next_turn_s_news(store: SessionStore) -> None:
    """The bug, named: after a restart this conversation was told nothing at all."""
    session, agent_id = await _left_running(store)
    work = _registry()

    await _restart(store, work)

    shown = work.snapshot(session, announce=True)
    assert [(item.id, item.status, item.finished_since_last_turn) for item in shown] == [
        (agent_id, "failed", True)
    ]
    assert shown[0].progress == f"{CONTINUABLE}: {RESTARTED}"
    notices = _check(work, session)["finished"]
    assert [notice["id"] for notice in notices] == [agent_id]
    assert _check(work, session)["finished"] == [], "told once"
    report = work.result(agent_id).payload
    assert isinstance(report, dict)
    assert report["resumable"] is True
    assert report["summary"] == RESTARTED


async def test_how_long_it_ran_is_until_it_was_last_heard_from_not_until_the_restart(
    store: SessionStore,
) -> None:
    session, _agent_id = await _left_running(store, worked_for=240.0)
    work = _registry()
    await _restart(store, work)
    assert work.snapshot(session)[0].elapsed_seconds == pytest.approx(240.0)


async def test_a_helper_that_never_wrote_anything_ran_for_no_time(store: SessionStore) -> None:
    session, _agent_id = await _left_running(store, worked_for=None)
    work = _registry()
    await _restart(store, work)
    assert work.snapshot(session)[0].elapsed_seconds == 0.0


async def test_a_helper_s_helper_is_not_announced_to_the_conversation(store: SessionStore) -> None:
    """Its parent was a helper, which stopped with it; continuing that parent resumes it."""
    session, _agent_id = await _left_running(store, depth=2)
    work = _registry()
    await _restart(store, work)
    assert work.snapshot(session) == ()


async def test_a_restart_with_no_registry_marks_helpers_and_announces_nothing(
    store: SessionStore,
) -> None:
    _session, agent_id = await _left_running(store)
    await _restart(store, None)
    assert (await AgentStore(store).get(ACCOUNT, agent_id))["status"] == "interrupted"


async def test_an_interrupted_helper_is_continued_from_its_transcript(store: SessionStore) -> None:
    session, agent_id = await _left_running(store)
    work = _registry()
    await _restart(store, work)
    provider = ScriptedProvider([speaks("Read the rest.")])
    capabilities = Capabilities((HelpPack(), AgentsPack()), work=work)
    agents = AgentStore(store)
    capabilities.child = ChildRuntime(
        store, agents, ModelRegistry({"scripted": lambda _model: provider}), capabilities
    )
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=session)
    context = capabilities.context_for(scope)

    reopened = await _reopen(work, context, depth=0, agent_id=agent_id)
    finished = await work.wait(str(reopened["id"]), 30)

    assert finished.state is State.succeeded
    handed = [str(message.content) for message in provider.requests[-1].messages]
    assert "halfway" in handed


def _lost(work: Registry, **overrides: Any) -> None:
    now = datetime.now(UTC)
    work.record_lost(
        Brief(session_id="ses_1", kind=Kind.helper, role="reader", objective="Read"),
        work_id=overrides.get("work_id", "agt_1"),
        started_at=now,
        ended_at=now,
        detail=overrides.get("detail", "stopped"),
        payload=overrides.get("payload"),
    )


def test_recording_lost_work_twice_is_recording_it_once() -> None:
    work = _registry()
    _lost(work, detail="first")
    _lost(work, detail="second")
    assert [item.progress for item in work.snapshot("ses_1")] == ["first"]


def test_lost_work_wakes_nobody() -> None:
    """Listeners wake idle sessions; a restart that woke every one it interrupted stampedes."""
    work = _registry()
    told: list[str] = []

    async def listener(record: object) -> None:
        told.append(str(record))

    work.on_finished(listener)
    _lost(work, payload={"summary": "x"})
    assert told == []
    assert work.result("agt_1").tokens > 0


def test_nothing_is_announced_without_a_registry() -> None:
    stopped = Interrupted(
        id="agt_1",
        account_id=ACCOUNT,
        session_id="ses_1",
        role="reader",
        objective="Read",
        depth=1,
        started_at=STARTED,
        last_seen=STARTED,
    )
    assert announce_interrupted(None, (stopped,)) == ()
    assert announce_interrupted(_registry(), (stopped,)) == ("agt_1",)


async def _hanging_helper(store: SessionStore) -> tuple[Registry, str, str]:
    """A helper that is mid-task and stays that way until something stops it."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "hang-key")
    work = _registry()
    capabilities = Capabilities((HelpPack(), AgentsPack()), work=work)
    child = ChildRuntime(
        store,
        AgentStore(store),
        ModelRegistry({"scripted": lambda _m: ScriptedProvider()}),
        capabilities,
    )
    capabilities.child = child
    started = asyncio.Event()

    async def hang(*_args: object, **_kwargs: object) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {}

    child._loop = hang
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=str(created["id"]))
    spawned = await _spawn(
        work, capabilities.context_for(scope), depth=0, objective="Read it all", role="reader"
    )
    await started.wait()
    return work, str(created["id"]), str(spawned["id"])


async def test_a_helper_running_when_the_hub_shuts_down_is_announced_when_it_is_back(
    store: SessionStore,
) -> None:
    """The bug, named: a graceful restart cancels every task, and the helper was recorded
    as cancelled -- as though the person had stopped it -- so the next process found
    nothing running, announced nothing, and never offered to continue it."""
    work, session, agent_id = await _hanging_helper(store)

    await work.shutdown()
    assert (await AgentStore(store).get(ACCOUNT, agent_id))["status"] == "running"

    after = _registry()
    await _restart(store, after)
    [shown] = after.snapshot(session, announce=True)
    assert (shown.id, shown.progress) == (agent_id, f"{CONTINUABLE}: {RESTARTED}")


async def test_a_helper_somebody_cancels_is_still_recorded_as_cancelled(
    store: SessionStore,
) -> None:
    work, _session, agent_id = await _hanging_helper(store)

    work.cancel(agent_id)
    ended = await work.wait(agent_id, 30)

    assert ended.state is State.cancelled
    row = await AgentStore(store).get(ACCOUNT, agent_id)
    assert (row["status"], row["interrupted_reason"]) == ("interrupted", "cancelled")
    await work.shutdown()
