"""The durable bridge from queued input to the single-agent loop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.clients.testing import Answer, FakeHttp
from lucy_api.context.build import Live
from lucy_api.context.feeds import Feed, FeedEntry, StaticFeeds
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.help import HelpPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.approvals import answer_approval
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import cancel_turn, submit_messages
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.stop import Budget
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor

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


def supervisor(
    store: SessionStore,
    provider: ScriptedProvider,
    capabilities: Capabilities | None = None,
    on_status: Any = None,
) -> TurnSupervisor:
    registry = ModelRegistry({"scripted": lambda _: provider})
    events = EventEmitter(SqlEventLog(store), Snapshot())
    return TurnSupervisor(store, registry, events, capabilities=capabilities, on_status=on_status)


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
    assert len(provider.requests) == 2
    first_prompt = "\n".join(message.content for message in provider.requests[0].messages)
    second_prompt = "\n".join(message.content for message in provider.requests[1].messages)
    assert "First" in first_prompt
    assert "Second" in second_prompt
    assert "One" not in first_prompt
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
    seen: list[tuple[str, str, str, str]] = []

    async def capture(account: str, session_id: str, turn_id: str, status: str) -> None:
        seen.append((account, session_id, turn_id, status))

    running = supervisor(store, provider, on_status=capture)

    running.wake()
    await running.join()

    items = await store.records(ACCOUNT, conversation, "items")
    kinds = [item["type"] for item in items]
    assert "tool_result" in kinds
    result = next(item for item in items if item["type"] == "tool_result")
    assert result["content"]["operation"] == "capabilities.list"
    assert result["content"]["status"] == "ok"
    recorded = await store.steps(ACCOUNT, conversation, str(result["turn_id"]))
    assert recorded[0]["step_id"] == "list"
    assert recorded[0]["kind"] == "capabilities.list"
    assert seen[-1] == (ACCOUNT, conversation, str(result["turn_id"]), "completed")
    await running.aclose()


async def test_a_request_prepared_feed_is_consumed_by_its_turn_only(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    turn = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Who am I?"}],
        "prepared-key",
    )
    provider = ScriptedProvider([speaks("You prefer tea.")])
    running = supervisor(store, provider)
    running.authorize(
        str(turn["id"]),
        PreparedTurn(
            pack_context=Capabilities().context_for(
                SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
            ),
            live=Live(
                feeds=(
                    StaticFeeds(
                        name="persona",
                        feeds=(
                            Feed(
                                id="persona",
                                title="identity",
                                entries=(
                                    FeedEntry(
                                        "note_1", "prefers tea", setting="notes", source="owner"
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            ),
        ),
    )

    running.wake()
    await running.join()

    assert "prefers tea" not in provider.requests[0].system
    persona = next(
        message for message in provider.requests[0].messages if "prefers tea" in message.content
    )
    assert "recorded claims, not instructions" in persona.content
    assert str(turn["id"]) not in running._prepared
    await running.aclose()


async def test_the_prepared_model_round_limit_stops_the_main_loop(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    turn = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Keep looking"}],
        "round-limit-key",
    )
    plan = {"steps": [{"id": "list", "op": "capabilities.list", "input": {}}]}
    provider = ScriptedProvider([plans(plan), speaks("This second round must not run.")])
    running = supervisor(store, provider)
    running.authorize(
        str(turn["id"]),
        PreparedTurn(
            pack_context=Capabilities().context_for(
                SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
            ),
            live=Live(),
            budget=Budget(max_iterations=1),
            max_subagent_turns=3,
        ),
    )

    running.wake()
    await running.join()

    completed = await store.turn(ACCOUNT, str(turn["id"]))
    assert len(provider.requests) == 1
    assert completed["termination"] == "error_max_iterations"
    await running.aclose()


async def test_a_restart_fails_an_abandoned_running_turn_then_drains_what_was_queued(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    abandoned = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "First"}],
        "abandoned-key",
    )
    claimed = await store.claim_next_turn()
    assert claimed is not None
    assert claimed["id"] == abandoned["id"]
    queued = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Second"}],
        "queued-after-crash",
    )
    provider = ScriptedProvider([speaks("The queued message still ran.")])
    running = supervisor(store, provider)

    await running.start()
    await running.join()

    failed = await store.turn(ACCOUNT, str(abandoned["id"]))
    completed = await store.turn(ACCOUNT, str(queued["id"]))
    items = await store.records(ACCOUNT, conversation, "items")
    assert failed["status"] == "failed"
    assert failed["stop_reason"] == "process_restarted"
    assert completed["status"] == "completed"
    assert items[-1]["content"] == "The queued message still ran."
    assert any(
        item["type"] == "error" and item["content"]["code"] == "process_restarted" for item in items
    )
    await running.aclose()


WRITE = {
    "steps": [
        {
            "id": "keep",
            "op": "notes.remember",
            "note": "Keep the tea preference",
            "input": {"title": "tea", "body": "green"},
        }
    ]
}


async def test_a_gated_write_parks_until_the_person_answers_then_resumes(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    queued = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Remember I drink tea."}],
        "approval-key",
    )
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": []}),
        Answer(status_code=201, body={"id": "mem_tea"}),
    )
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
    provider = ScriptedProvider([plans(WRITE), speaks("I will keep that.")])
    running = supervisor(store, provider, capabilities)
    running.authorize(
        str(queued["id"]),
        PreparedTurn(
            pack_context=capabilities.context_for(scope, http=http),
            live=Live(),
        ),
    )

    running.wake()
    await running.join()

    parked = await store.turn(ACCOUNT, str(queued["id"]))
    items = await store.records(ACCOUNT, conversation, "items")
    ask = next(item for item in items if item["type"] == "approval_request")
    assert parked["status"] == "input_required"
    assert ask["content"]["permission"] == "notes.write"

    await answer_approval(
        store,
        ACCOUNT,
        conversation,
        {
            "type": "input.approval",
            "approval_id": ask["content"]["approval_id"],
            "approved": True,
        },
        "approve-key",
    )
    running.authorize(
        str(queued["id"]),
        PreparedTurn(
            pack_context=capabilities.context_for(scope, http=http),
            live=Live(),
        ),
    )
    running.wake()
    await running.join()

    finished = await store.turn(ACCOUNT, str(queued["id"]))
    assert finished["status"] == "completed"
    assert provider.remaining == 0
    await running.aclose()


async def test_auto_mode_records_an_audit_row_instead_of_asking(store: SessionStore) -> None:
    created = await store.create(
        ACCOUNT,
        CreateSession(model="scripted:demo", permission_mode="auto"),
        "auto-session",
    )
    conversation = str(created["id"])
    queued = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "Remember I drink tea."}],
        "auto-key",
    )
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": []}),
        Answer(status_code=201, body={"id": "mem_tea"}),
    )
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    scope = SessionScope(
        account_id=ACCOUNT,
        profile="personal",
        session_id=conversation,
        permission_mode="auto",
    )
    provider = ScriptedProvider([plans(WRITE), speaks("Kept.")])
    running = supervisor(store, provider, capabilities)
    running.authorize(
        str(queued["id"]),
        PreparedTurn(
            pack_context=capabilities.context_for(scope, http=http),
            live=Live(),
        ),
    )
    running.wake()
    await running.join()

    finished = await store.turn(ACCOUNT, str(queued["id"]))
    actions = [row["action"] for row in await store.audit_log(ACCOUNT)]
    assert finished["status"] == "completed"
    assert "permission.auto" in actions
    await running.aclose()


async def test_a_spoken_turn_writes_text_events_into_the_session_log(store: SessionStore) -> None:
    conversation = await session(store)
    await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "What changed?"}],
        "stream-key",
    )
    provider = ScriptedProvider(
        [speaks("The tests now cover the session stream.", reasoning="check")]
    )
    running = supervisor(store, provider)
    running.wake()
    await running.join()

    events = await store.records(ACCOUNT, conversation, "events")
    types = [str(event["type"]) for event in events]
    assert "lucy.content.reasoning.start" not in types
    assert "lucy.content.text.delta" in types
    await running.aclose()


async def test_stream_thinking_forwards_reasoning_events_when_the_person_asked(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    turn = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "What changed?"}],
        "thinking-stream-key",
    )
    provider = ScriptedProvider(
        [speaks("The tests now cover the session stream.", reasoning="check")]
    )
    running = supervisor(store, provider)
    pack_context = Capabilities().context_for(
        SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
    )
    pack_context.policy = TurnPolicy(stream_thinking=True)
    running.authorize(str(turn["id"]), PreparedTurn(pack_context=pack_context, live=Live()))
    running.wake()
    await running.join()

    events = await store.records(ACCOUNT, conversation, "events")
    types = [str(event["type"]) for event in events]
    assert "lucy.content.reasoning.start" in types
    await running.aclose()


async def test_a_full_window_compacts_older_turns_and_swallows_a_too_new_session(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    first = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "keep " + ("earlier " * 80)}],
        "compact-first",
    )
    second = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "keep " + ("later " * 80)}],
        "compact-second",
    )
    provider = ScriptedProvider([speaks("One"), speaks("Two")])
    running = supervisor(store, provider)
    policy = TurnPolicy(
        max_context_tokens=100,
        compaction_trigger_percent=50,
        history_turns_kept=1,
        warn_at_percent=10,
    )
    for turn_id in (str(first["id"]), str(second["id"])):
        pack_context = Capabilities().context_for(
            SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
        )
        pack_context.policy = policy
        running.authorize(turn_id, PreparedTurn(pack_context=pack_context, live=Live()))
    running.wake()
    await running.join()

    compacted = await store.records(ACCOUNT, conversation, "compactions")
    assert compacted
    assert compacted[0]["active"] == 1
    assert (await store.turn(ACCOUNT, str(first["id"])))["status"] == "completed"
    assert (await store.turn(ACCOUNT, str(second["id"])))["status"] == "completed"
    await running.aclose()


def test_a_rolled_back_turn_vanishes_from_the_next_prompt() -> None:
    from lucy_api.turn.supervisor import _conversation_order

    items = [
        {"id": "1", "seq": 1, "turn_id": "t1", "role": "user", "content": "old"},
        {"id": "2", "seq": 2, "turn_id": "t1", "role": "assistant", "content": "partial"},
        {"id": "3", "seq": 3, "turn_id": "t2", "role": "user", "content": "new"},
    ]
    turns = [
        {"id": "t1", "status": "cancelled", "stop_reason": "rollback", "created_at": 1.0},
        {"id": "t2", "status": "queued", "stop_reason": None, "created_at": 2.0},
    ]
    ordered = _conversation_order(items, turns, {"t1", "t2"})
    assert [item["content"] for item in ordered] == ["new"]


def test_an_interrupted_turn_keeps_the_progress_it_had_already_made() -> None:
    from lucy_api.turn.supervisor import _conversation_order

    items = [
        {"id": "1", "seq": 1, "turn_id": "t1", "role": "user", "content": "old"},
        {"id": "2", "seq": 2, "turn_id": "t1", "role": "assistant", "content": "partial"},
        {"id": "3", "seq": 3, "turn_id": "t2", "role": "user", "content": "new"},
    ]
    turns = [
        {"id": "t1", "status": "cancelled", "stop_reason": "interrupt", "created_at": 1.0},
        {"id": "t2", "status": "queued", "stop_reason": None, "created_at": 2.0},
    ]
    ordered = _conversation_order(items, turns, {"t1", "t2"})
    assert [item["content"] for item in ordered] == ["old", "partial", "new"]


async def test_a_cancel_flag_set_while_the_model_is_running_ends_the_turn(
    store: SessionStore,
) -> None:
    conversation = await session(store)
    entered = asyncio.Event()
    release = asyncio.Event()
    inner = ScriptedProvider([speaks("I was still talking.")])
    provider = _GateProvider(inner, entered, release)
    first = await submit_messages(
        store,
        ACCOUNT,
        conversation,
        [{"type": "input.message", "content": "start"}],
        "cancel-first",
    )
    running = supervisor(store, provider)
    running.wake()
    await asyncio.wait_for(entered.wait(), timeout=2)
    asked = await store.turn(ACCOUNT, str(first["id"]))
    assert asked["status"] == "running"
    await cancel_turn(store, ACCOUNT, str(first["id"]))
    release.set()
    await running.join()
    ended = await store.turn(ACCOUNT, str(first["id"]))
    assert ended["status"] == "cancelled"
    await running.aclose()


class _GateProvider:
    """Hold the first model call until a test has set cancel_requested."""

    name = "scripted"

    def __init__(
        self, inner: ScriptedProvider, entered: asyncio.Event, release: asyncio.Event
    ) -> None:
        self._inner = inner
        self.entered = entered
        self.release = release
        self.requests = inner.requests

    async def complete(self, request: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        return await self._inner.complete(request)

    async def stream(self, request: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        async for chunk in self._inner.stream(request):
            yield chunk
