"""A turn that fails says why, to the person, where they are reading.

Seen live on clyde:haiku: the turn after a restart ended `failed / error_during_execution`
with no reply at all. clyde had answered 502 -- the CLI stopped at its turn cap -- and the
hub's sentence for it, "the model was unavailable: ...", went to a log line and nowhere else.
The transcript held the person's question and nothing after it, and the turn's `error_code`
was empty. Only a model that could not even be *built* left an error item; one that failed
mid-conversation left silence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, fails, flakes, plans, speaks
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn import supervisor as supervising
from lucy_api.turn.loop import Outcome
from lucy_api.turn.stop import Termination
from lucy_api.turn.supervisor import MODEL_UNAVAILABLE, NO_REASON, TURN_FAILED, TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

ACCOUNT = "acct_failure_said"
LIMIT = "clyde answered 502: claude stopped with error_max_turns after 2 turns (max_turns)"
READ_DOCS = {"steps": [{"id": "docs", "op": "help.docs", "input": {"topic": "notes"}}]}


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
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


async def _turn(
    store: SessionStore, provider: ScriptedProvider, model: str = "scripted:demo"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one turn to its end; return the turn row and the last transcript item."""
    created = await store.create(ACCOUNT, CreateSession(model=model), "session-key")
    conversation = str(created["id"])
    queued = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "Hello"}], "q"
    )
    running = TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _model: provider}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities=Capabilities((HelpPack(),)),
    )
    running.wake()
    await running.join()
    await running.aclose()
    items = await store.records(ACCOUNT, conversation, "items")
    return await store.turn(ACCOUNT, str(queued["id"])), items[-1]


async def test_a_model_that_becomes_unavailable_mid_turn_is_said_to_the_person(
    store: SessionStore,
) -> None:
    """The bug, named: this turn ended with the person's question and nothing after it."""
    turn, last = await _turn(store, ScriptedProvider([plans(READ_DOCS), flakes(LIMIT)]))

    assert turn["status"] == "failed"
    assert turn["error_code"] == MODEL_UNAVAILABLE
    assert last["type"] == "error"
    assert last["content"]["code"] == MODEL_UNAVAILABLE
    assert "error_max_turns" in last["content"]["detail"]


async def test_any_other_failure_is_said_too_under_its_own_code(store: SessionStore) -> None:
    turn, last = await _turn(store, ScriptedProvider([fails("that key was revoked")]))

    assert turn["status"] == "failed"
    assert turn["error_code"] == TURN_FAILED
    assert (last["type"], last["content"]["code"]) == ("error", TURN_FAILED)
    assert last["content"]["detail"]


async def test_a_failure_with_no_reason_recorded_says_so_rather_than_nothing(
    store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def silent(_turn: object) -> Outcome:
        return Outcome(termination=Termination.failed)

    monkeypatch.setattr(supervising, "run_turn", silent)
    turn, last = await _turn(store, ScriptedProvider())

    assert turn["error_code"] == TURN_FAILED
    assert last["content"] == {"code": TURN_FAILED, "detail": NO_REASON}


async def test_a_model_that_cannot_be_built_records_its_code_on_the_turn(
    store: SessionStore,
) -> None:
    turn, last = await _turn(store, ScriptedProvider(), model="openai:gpt-5")
    assert turn["error_code"] == MODEL_UNAVAILABLE
    assert last["content"]["code"] == MODEL_UNAVAILABLE


async def test_a_turn_that_succeeds_carries_no_error(store: SessionStore) -> None:
    turn, last = await _turn(store, ScriptedProvider([speaks("Hi.")]))
    assert turn["status"] == "completed"
    assert turn["error_code"] is None
    assert last["type"] == "message"
