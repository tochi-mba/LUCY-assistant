"""The durable bridge from queued input to the single-agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping


ACCOUNT = "acct_turn_supervisor"


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


async def session(store: SessionStore, *, model: str = "scripted:demo") -> str:
    created = await store.create(ACCOUNT, CreateSession(model=model), "session-key")
    return str(created["id"])


def supervisor(store: SessionStore, provider: ScriptedProvider) -> TurnSupervisor:
    registry = ModelRegistry({"scripted": lambda _: provider})
    events = EventEmitter(SqlEventLog(store), Snapshot())
    return TurnSupervisor(store, registry, events)


async def test_queued_input_becomes_an_assistant_item_and_a_completed_turn(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    queued = await store.claim_next_turn()
    assert queued is None
    turn = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "What changed?"}],
        "input-key",
    )
    provider = ScriptedProvider([speaks("The tests now cover the session stream.")])
    running = supervisor(store, provider)

    running.wake()
    await running.join()

    completed = await store.turn(ACCOUNT, str(turn["id"]))
    items = await store.records(ACCOUNT, conversation, "items")
    assert completed["status"] == "completed"
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert items[-1]["content"] == "The tests now cover the session stream."
    assert provider.requests[0].messages[-1].content == "What changed?"
    assert claimed_session_is_named(provider.requests[0].system)
    await running.aclose()


def claimed_session_is_named(system: str) -> bool:
    """The live runner's prompt is the context engine's, not a capabilities-only prefix."""
    return "context:" in system.lower() or "session" in system.lower()


async def test_one_session_runs_queued_messages_in_order_without_parallel_writers(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    first = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "First"}],
        "first-key",
    )
    second = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Second"}],
        "second-key",
    )
    provider = ScriptedProvider([speaks("One"), speaks("Two")])
    running = supervisor(store, provider)

    running.wake()
    await running.join()

    assert (await store.turn(ACCOUNT, str(first["id"])))["status"] == "completed"
    assert (await store.turn(ACCOUNT, str(second["id"])))["status"] == "completed"
    assert [request.messages[-1].content for request in provider.requests] == ["First", "Second"]
    assert (await store.get(ACCOUNT, conversation))["status"] == "idle"
    await running.aclose()


async def test_an_unconfigured_model_records_a_safe_failure_instead_of_leaving_work_running(
    store: SessionStore,
) -> None:
    conversation = await session(store, model="openai:gpt-5")
    turn = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Hello"}],
        "missing-model-key",
    )
    running = supervisor(store, ScriptedProvider())

    running.wake()
    await running.join()

    outcome = await store.turn(ACCOUNT, str(turn["id"]))
    items = await store.records(ACCOUNT, conversation, "items")
    assert outcome["status"] == "failed"
    assert items[-1]["content"]["code"] == "model_unavailable"
    assert "credential" not in items[-1]["content"]["detail"]
    await running.aclose()


async def test_a_plan_runs_the_help_pack_and_records_the_tool_result(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "What can you do?"}],
        "plan-key",
    )
    plan = {"steps": [{"id": "list", "op": "capabilities.list", "input": {}}]}
    provider = ScriptedProvider([plans(plan), speaks("I can list what is connected.")])
    running = supervisor(store, provider)

    running.wake()
    await running.join()

    items = await store.records(ACCOUNT, conversation, "items")
    kinds = [item["type"] for item in items]
    assert "tool_result" in kinds
    result = next(item for item in items if item["type"] == "tool_result")
    assert result["content"]["operation"] == "capabilities.list"
    assert result["content"]["status"] == "ok"
    await running.aclose()
