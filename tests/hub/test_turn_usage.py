"""What a finished turn writes down about what it cost.

The `turns` table has held `input_tokens`, `output_tokens`, `cost_micros` and `iterations`
since the first migration, and nothing ever wrote them. Every round already carried its own
`Usage`; nothing added them up. So `GET /usage` answered zero for every session ever recorded,
and "cost per turn" -- one of the six metrics `docs/baseline.md` lists as a gate for the
typed-decision work -- had no denominator. Found by running real turns and reading zeros back
off all of them.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.model.types import Usage
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.sessions.usage import session_usage
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor, _spend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_turn_usage"


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


async def a_turn(store: SessionStore, provider: Any) -> tuple[str, TurnSupervisor]:
    """One session, one queued message, and a supervisor ready to run it."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    conversation = str(created["id"])
    await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Hello."}],
        "input-key",
    )
    running = TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _: provider}),
        EventEmitter(SqlEventLog(store), Snapshot()),
    )
    return conversation, running


async def test_a_finished_turn_records_what_it_spent(store: SessionStore) -> None:
    spent = Usage(input_tokens=1200, output_tokens=64, cache_read_tokens=800)
    conversation, running = await a_turn(
        store, ScriptedProvider([speaks("Hello back.", usage=spent)])
    )
    running.wake()
    await running.join()

    usage = await session_usage(store, ACCOUNT, conversation)
    assert usage["turns"] == 1
    assert usage["turn_input_tokens"] == 1200
    assert usage["turn_output_tokens"] == 64
    assert usage["turn_cache_read_tokens"] == 800
    assert usage["turn_iterations"] == 1
    await running.aclose()


async def test_the_cache_read_is_kept_apart_from_the_input(store: SessionStore) -> None:
    """Folding it into `input_tokens` would hide the one number that says whether the cached
    prefix survived a change -- which is the whole argument for ordering a prompt by
    volatility, and what `model.types.Usage` says in its own docstring."""
    spent = Usage(input_tokens=100, output_tokens=10, cache_read_tokens=9_000)
    conversation, running = await a_turn(store, ScriptedProvider([speaks("Hi.", usage=spent)]))
    running.wake()
    await running.join()

    usage = await session_usage(store, ACCOUNT, conversation)
    assert usage["turn_cache_read_tokens"] == 9_000
    assert usage["turn_input_tokens"] == 100, "the cache read is not folded in"
    await running.aclose()


def test_every_round_is_counted_not_only_the_last() -> None:
    """A turn that plans, runs its steps and then answers spends more than once, and the row
    has to be the sum. Asserted against the summing itself: driving two real rounds through a
    supervisor needs a capability wired up, which would test the executor rather than this."""
    outcome = SimpleNamespace(
        rounds=(
            SimpleNamespace(
                usage=Usage(input_tokens=1000, output_tokens=20, cache_read_tokens=500)
            ),
            SimpleNamespace(
                usage=Usage(input_tokens=1500, output_tokens=40, cache_read_tokens=900)
            ),
        )
    )
    spend = _spend(outcome)
    assert spend.input_tokens == 2500
    assert spend.output_tokens == 60
    assert spend.cache_read_tokens == 1400
    assert spend.iterations == 2


def test_a_round_that_reported_no_usage_is_skipped_rather_than_guessed() -> None:
    """A provider that sends no usage block leaves `Round.usage` at None. Counting it as zero
    is right; counting the round is still right, because it happened."""
    outcome = SimpleNamespace(
        rounds=(
            SimpleNamespace(usage=None),
            SimpleNamespace(usage=Usage(input_tokens=7, output_tokens=3)),
        )
    )
    spend = _spend(outcome)
    assert spend.input_tokens == 7
    assert spend.iterations == 2


async def test_a_turn_that_never_reached_a_model_records_zero(store: SessionStore) -> None:
    """`spent` is optional so the callers that end a turn without ever reaching a model -- a
    cancel before the first round, an abandoned turn swept up at startup -- do not have to
    invent a zero."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    conversation = str(created["id"])
    turn = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Hello."}],
        "input-key",
    )
    await store.finish_turn(ACCOUNT, str(turn["id"]), "cancelled")

    usage = await session_usage(store, ACCOUNT, conversation)
    assert usage["turn_input_tokens"] == 0
    assert usage["turn_output_tokens"] == 0
