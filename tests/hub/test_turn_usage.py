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

from lucy_api.context.build import Live
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.model.types import Usage
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore, TurnSpend
from lucy_api.sessions.turns import submit_messages
from lucy_api.sessions.usage import session_usage
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.stop import WRAP_UP, Budget, Spent, should_stop, warning_for
from lucy_api.turn.supervisor import (
    SESSION_BUDGET,
    PreparedTurn,
    TurnSupervisor,
    _remaining,
    _spend,
)

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


# --- every run of a turn, and the session's own totals -------------------------------------
#
# Read off a live session after two turns: `GET /usage` said `input_tokens: 0` beside
# `turn_input_tokens: 54,647`. The session's columns were never written. And a turn that parks
# for approval runs the loop twice: the first run's cost was never written, because the turn
# had not finished, and the second overwrote the row. `session_token_budget`, "a hard cap on
# tokens one session may spend", was handed to the loop as a cap on one turn.


async def test_the_session_s_totals_are_the_sum_of_its_turns(store: SessionStore) -> None:
    """The bug, named: the session said zero beside the true sum over its turns."""
    conversation, running = await a_turn(
        store,
        ScriptedProvider(
            [
                speaks("One.", usage=Usage(input_tokens=1_000, output_tokens=50)),
                speaks("Two.", usage=Usage(input_tokens=2_000, output_tokens=70)),
            ]
        ),
    )
    running.wake()
    await running.join()
    await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "Again."}], "k2"
    )
    running.wake()
    await running.join()

    usage = await session_usage(store, ACCOUNT, conversation)
    assert (usage["input_tokens"], usage["output_tokens"]) == (3_000, 120)
    assert (usage["turn_input_tokens"], usage["turn_output_tokens"]) == (3_000, 120)
    await running.aclose()


async def test_a_turn_that_parked_for_approval_counts_both_of_its_runs(
    store: SessionStore,
) -> None:
    """The bug, named: the run before the approval was never written, and the run after it
    overwrote the row."""
    parked = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    conversation = str(parked["id"])
    turn = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "Keep it."}], "k1"
    )
    turn_id = str(turn["id"])
    await store.record_spend(ACCOUNT, turn_id, TurnSpend(input_tokens=500, output_tokens=20))
    await store.finish_turn(
        ACCOUNT,
        turn_id,
        "completed",
        "success",
        "end_turn",
        spent=TurnSpend(input_tokens=700, output_tokens=30, iterations=1),
    )

    usage = await session_usage(store, ACCOUNT, conversation)
    assert (usage["turn_input_tokens"], usage["turn_output_tokens"]) == (1_200, 50)
    assert (usage["input_tokens"], usage["output_tokens"]) == (1_200, 50)


async def test_the_budget_is_the_conversation_s_and_a_spent_one_gets_no_model_round(
    store: SessionStore,
) -> None:
    """The bug, named: `session_token_budget` capped each turn on its own."""
    conversation, running = await a_turn(
        store,
        ScriptedProvider([speaks("One.", usage=Usage(input_tokens=950, output_tokens=50))]),
    )
    capabilities = Capabilities((HelpPack(),))
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)

    def prepared() -> PreparedTurn:
        context = capabilities.context_for(scope)
        context.policy = TurnPolicy(session_token_budget=1_000)
        return PreparedTurn(pack_context=context, live=Live(), budget=Budget(max_tokens=1_000))

    turns = await store.records(ACCOUNT, conversation, "turns")
    running.authorize(str(turns[0]["id"]), prepared())
    running.wake()
    await running.join()

    again = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "More."}], "k2"
    )
    running.authorize(str(again["id"]), prepared())
    running.wake()
    await running.join()

    second = await store.turn(ACCOUNT, str(again["id"]))
    assert (second["status"], second["termination"]) == ("failed", "error_max_budget")
    assert second["error_code"] == SESSION_BUDGET
    items = await store.records(ACCOUNT, conversation, "items")
    [said] = [item["content"] for item in items if item["type"] == "error"]
    assert said["code"] == SESSION_BUDGET
    assert "spent its budget of 1,000 tokens" in said["detail"]
    assert (await session_usage(store, ACCOUNT, conversation))["input_tokens"] == 950
    await running.aclose()


def test_the_remaining_budget_is_what_the_conversation_has_left() -> None:
    session = {"input_tokens": 600, "output_tokens": 100}
    assert _remaining(Budget(max_tokens=1_000), session) == Budget(max_tokens=300)
    assert _remaining(Budget(max_tokens=700), session) is None
    assert _remaining(Budget(), session) == Budget(), "no cap stays no cap"


def test_the_stop_rule_names_the_conversation_not_the_turn() -> None:
    verdict = should_stop(Budget(max_tokens=100), Spent(tokens=100))
    assert verdict.detail == "stopped after 100 tokens, all this conversation had left"
    assert warning_for(Budget(max_tokens=100), Spent(tokens=90)).endswith(
        f"left in this conversation's budget: {WRAP_UP}"
    )
