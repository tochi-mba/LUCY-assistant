"""A helper is a real child run: clean context, capped return, durable roster, inbox."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.agents.journal import JournalLive
from lucy_api.agents.runtime import ChildRuntime, _brief_text
from lucy_api.agents.store import AgentStore
from lucy_api.agents.types import RESULT_TOKEN_CAP, Delegation, capped_summary
from lucy_api.core.errors import LucyError
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.model.types import Reply
from lucy_api.packs.agents import MAX_DEPTH, AgentsPack, _message, _reopen, _spawn
from lucy_api.packs.base import State as PackState
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.prompt.docs import capability_doc
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import NewItem, SessionStore
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.work.registry import Registry
from lucy_api.work.types import Brief, Kind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from lucy_api.model.types import Request
    from lucy_api.packs.context import PackContext

ACCOUNT = "acct_agents"
STRANGER = "acct_other"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore, *, model: str = "scripted:demo") -> str:
    created = await store.create(ACCOUNT, CreateSession(model=model), "session-key")
    return str(created["id"])


def parent_context(
    session_id: str,
    *,
    capabilities: Capabilities,
    depth: int = 0,
    workspace: str = "",
) -> PackContext:
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=session_id, depth=depth)
    context = capabilities.context_for(scope)
    context.workspace_environment_id = workspace
    return context


def runtime_for(
    store: SessionStore,
    provider: ScriptedProvider,
    *,
    work: Registry | None = None,
) -> tuple[ChildRuntime, Capabilities, AgentStore]:
    capabilities = Capabilities((HelpPack(), AgentsPack()), work=work)
    agents = AgentStore(store)
    models = ModelRegistry({"scripted": lambda _model: provider})
    child = ChildRuntime(store, agents, models, capabilities)
    capabilities.child = child
    return child, capabilities, agents


async def test_a_helper_runs_the_loop_and_returns_a_capped_summary(store: SessionStore) -> None:
    session = await a_session(store)
    provider = ScriptedProvider([speaks("Paris in June.")])
    child, capabilities, agents = runtime_for(store, provider)
    result = await child.run(
        parent_context(session, capabilities=capabilities, workspace="env_1"),
        objective="When is the tour in Europe?",
        role="researcher",
    )

    assert result["status"] == "ok"
    assert result["summary"] == "Paris in June."
    assert result["permission_mode"] == "plan"
    row = await agents.get(ACCOUNT, result["agent_id"])
    assert row["status"] == "completed"
    assert row["role"] == "researcher"
    tasks = await agents.tasks(ACCOUNT, session)
    assert tasks[0].status == "completed"
    assert tasks[0].title == "When is the tour in Europe?"


async def test_a_helper_does_not_see_the_parent_transcript(store: SessionStore) -> None:
    session = await a_session(store)
    await store.append(ACCOUNT, session, NewItem("message", "user", "parent secret"))
    provider = ScriptedProvider([speaks("noted")])
    child, capabilities, _agents = runtime_for(store, provider)
    await child.run(
        parent_context(session, capabilities=capabilities),
        objective="Look this up",
        role="helper",
    )

    shown = " ".join(
        message.content for request in provider.requests for message in request.messages
    )
    assert "parent secret" not in shown
    assert "Look this up" in shown
    parent_items = [
        item for item in await store.records(ACCOUNT, session, "items") if not item.get("agent_id")
    ]
    assert parent_items[0]["content"] == "parent secret"


async def test_an_oversized_return_is_clipped_and_named(store: SessionStore) -> None:
    session = await a_session(store)
    huge = "word " * (RESULT_TOKEN_CAP + 200)
    provider = ScriptedProvider([speaks(huge)])
    child, capabilities, _agents = runtime_for(store, provider)
    result = await child.run(
        parent_context(session, capabilities=capabilities),
        objective="Summarise",
        role="helper",
    )

    assert result["tokens"] == RESULT_TOKEN_CAP
    assert result["notice"].startswith("showing")
    assert result["summary"].endswith("…")


async def test_a_helper_that_cannot_answer_is_a_failed_result_not_an_exception(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    provider = ScriptedProvider([])
    child, capabilities, agents = runtime_for(store, provider)
    result = await child.run(
        parent_context(session, capabilities=capabilities),
        objective="Anything",
        role="helper",
    )

    assert result["status"] == "failed"
    assert "ScriptExhaustedError" in result["summary"]
    assert (await agents.get(ACCOUNT, result["agent_id"]))["status"] == "failed"


async def test_a_parent_message_arrives_at_the_next_round_not_mid_tool(
    store: SessionStore,
) -> None:
    session = await a_session(store)

    class Recording(ScriptedProvider):
        async def complete(self, request: Request) -> Reply:
            reply = await super().complete(request)
            if self.calls == 1:
                running = await agents.running(ACCOUNT, session)
                await child.send(parent, str(running[0]["id"]), "focus on dates")
            return reply

    provider = Recording(
        [
            plans({"steps": [{"id": "s", "op": "capabilities.list", "input": {}}]}),
            speaks("Dates only."),
        ]
    )
    child, capabilities, agents = runtime_for(store, provider)
    parent = parent_context(session, capabilities=capabilities)
    result = await child.run(parent, objective="Find dates", role="researcher")

    assert result["status"] == "ok"
    assert result["summary"] == "Dates only."
    second = provider.requests[1]
    blob = second.system + "\n" + "\n".join(message.content for message in second.messages)
    assert "focus on dates" in blob


async def test_steering_a_finished_helper_says_so(store: SessionStore) -> None:
    session = await a_session(store)
    provider = ScriptedProvider([speaks("done")])
    child, capabilities, _agents = runtime_for(store, provider)
    parent = parent_context(session, capabilities=capabilities)
    result = await child.run(parent, objective="One job", role="helper")
    sent = await child.send(parent, result["agent_id"], "do more")
    empty = await child.send(parent, result["agent_id"], "   ")

    assert sent["status"] == "finished"
    assert empty["status"] == "finished"


async def test_a_restart_marks_running_helpers_interrupted_and_releases_the_journal(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(
        ACCOUNT, session, role="helper", objective="still going", depth=1
    )
    await agents.add_task(ACCOUNT, session, title="still going", agent_id=agent_id)
    interrupted = await agents.interrupt_running()

    assert interrupted == (agent_id,)
    assert (await agents.get(ACCOUNT, agent_id))["status"] == "interrupted"
    assert (await agents.tasks(ACCOUNT, session))[0].status == "pending"


async def test_the_journal_is_this_account_s_and_a_stranger_cannot_attach_a_helper(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="secret", depth=1)
    await agents.send_mail(ACCOUNT, agent_id, "hello")
    await agents.send_mail(ACCOUNT, agent_id, "   ")
    mail = await agents.drain_mail(ACCOUNT, agent_id)
    await agents.add_task(ACCOUNT, session, title="secret", agent_id=agent_id)
    await agents.add_task(
        ACCOUNT,
        session,
        title="blocked work",
        agent_id=agent_id,
        status="pending",
        depends_on="1,2",
    )

    assert mail == ("hello",)
    with pytest.raises(LucyError) as caught:
        await agents.get(STRANGER, agent_id)
    assert caught.value.code == "not-found"
    with pytest.raises(LucyError):
        await agents.send_mail(STRANGER, agent_id, "nope")
    with pytest.raises(LucyError):
        await agents.running(STRANGER, session)
    live = JournalLive(agents, ACCOUNT)
    snapshots = await live.fetch(session)
    assert snapshots[0].claimed_by == agent_id
    assert snapshots[1].blocked_by == ("1", "2")


async def test_completing_a_task_that_is_not_this_session_is_a_miss(store: SessionStore) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    with pytest.raises(LucyError):
        await agents.complete_task(ACCOUNT, session, 99)
    other = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "other-key")
    task = await agents.add_task(ACCOUNT, str(other["id"]), title="elsewhere", status="pending")
    with pytest.raises(LucyError):
        await agents.complete_task(ACCOUNT, session, task)


async def test_spawn_records_the_brief_and_starts_work(store: SessionStore) -> None:
    session = await a_session(store)
    provider = ScriptedProvider([speaks("found it")])
    work = Registry(now=lambda: datetime.now(UTC))
    _child, capabilities, _agents = runtime_for(store, provider, work=work)
    context = parent_context(session, capabilities=capabilities)
    started = await _spawn(work, context, depth=0, objective="Find the date", role="researcher")
    assert started["state"] == "running"
    assert str(started["id"]).startswith("agt_")
    fetched = await work.wait(str(started["id"]), 30)
    payload = fetched.payload
    assert isinstance(payload, dict)
    assert payload["summary"] == "found it"


async def test_spawn_refusals_are_sentences(store: SessionStore) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    capabilities = Capabilities((AgentsPack(),), work=work)
    context = parent_context(session, capabilities=capabilities)
    assert (await _spawn(work, context, depth=0, objective="  ", role="x"))["status"] == ("invalid")
    assert (await _spawn(work, context, depth=MAX_DEPTH, objective="go", role="x"))[
        "status"
    ] == "too_deep"
    assert (await _spawn(work, context, depth=0, objective="go", role="x"))["status"] == (
        "not_configured"
    )
    context.child = runtime_for(store, ScriptedProvider([speaks("x")]), work=work)[0]
    assert (await _spawn(work, context, depth=0, objective="go", role="x"))["status"] == (
        "at_capacity"
    )


async def test_spawn_stops_at_the_configured_helper_cap(store: SessionStore) -> None:
    session = await a_session(store)
    occupied = Registry(now=lambda: datetime.now(UTC))

    class _Slot:
        kind = Kind.helper

    occupied.running = lambda _session_id: (_Slot(),)  # type: ignore[method-assign]
    child, capabilities, _agents = runtime_for(
        store, ScriptedProvider([speaks("x")]), work=occupied
    )
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    context.policy = TurnPolicy(agent_max_concurrent=1)
    refused = await _spawn(occupied, context, depth=0, objective="another", role="two")
    assert refused["status"] == "at_capacity"
    assert "cap reached" in refused["message"]

    class _Prepared:
        discarded: tuple[str, int] | None = None

        async def reopen(
            self, _context: object, _handle: str, return_schema: str = ""
        ) -> dict[str, Any]:
            del return_schema
            return {
                "agent_id": "agt_new",
                "task_id": 1,
                "objective": "continue",
                "role": "one",
            }

        async def discard_setup(self, _context: object, agent_id: str, task_id: int) -> None:
            self.discarded = (agent_id, task_id)

    prepared = _Prepared()
    context.child = prepared  # type: ignore[assignment]
    again = await _reopen(occupied, context, depth=0, agent_id="agt_old")
    assert again["status"] == "at_capacity"
    assert prepared.discarded == ("agt_new", 1)


async def test_a_child_is_not_offered_spawn() -> None:
    pack = AgentsPack()
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities((pack,), work=work)
    context = capabilities.context_for(
        SessionScope(account_id=ACCOUNT, profile="personal", session_id="ses_x", agent_id="agt_1")
    )
    names = [operation.name for operation in pack.operations(context)]
    assert "agents.spawn" not in names
    assert "agents.message" in names
    assert "journal.claim" in names
    assert pack.operations(parent_context("ses_x", capabilities=Capabilities((pack,)))) == ()
    assert pack.setup() is None
    assert pack.docs == capability_doc("agents")
    assert pack.permissions()[0].id == "agents.delegate"


async def test_message_refusals_name_the_fix(store: SessionStore) -> None:
    session = await a_session(store)
    capabilities = Capabilities((AgentsPack(),))
    context = parent_context(session, capabilities=capabilities)
    assert (await _message(context, agent_id="agt_1", body="hi"))["status"] == "not_configured"
    child, capabilities, _agents = runtime_for(store, ScriptedProvider([speaks("x")]))
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    assert (await _message(context, agent_id="  ", body="hi"))["status"] == "invalid"
    with pytest.raises(LucyError) as caught:
        await _message(context, agent_id="agt_missing", body="hi")
    assert caught.value.code == "not-found"


async def test_the_pack_probe_counts_running_helpers() -> None:
    pack = AgentsPack()
    empty = parent_context("ses_x", capabilities=Capabilities((pack,)))
    assert (await pack.probe(empty)).state is PackState.not_configured
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities((pack,), work=work)
    context = parent_context("ses_x", capabilities=capabilities)
    probed = await pack.probe(context)
    assert probed.state is PackState.ready
    assert "no helpers" in probed.detail


def test_the_brief_names_every_field_the_child_was_handed() -> None:
    text = _brief_text(
        Delegation(
            objective="Check the dates",
            role="reviewer",
            constraints="do not invent",
            boundaries="read-only",
            guidance="prefer primary sources",
        )
    )
    assert "do not invent" in text
    assert "read-only" in text
    assert "prefer primary sources" in text


def test_a_short_summary_is_not_clipped() -> None:
    text, tokens, notice = capped_summary("short")
    assert text == "short"
    assert tokens > 0
    assert notice == ""


def test_parent_and_child_item_logs_are_split() -> None:
    from lucy_api.turn.supervisor import _items_for

    rows: list[dict[str, Any]] = [
        {"agent_id": None, "id": "p"},
        {"agent_id": "agt_1", "id": "c"},
    ]
    assert [row["id"] for row in _items_for(rows, "")] == ["p"]
    assert [row["id"] for row in _items_for(rows, "agt_1")] == ["c"]


async def test_an_empty_steer_to_a_running_helper_is_invalid(store: SessionStore) -> None:
    session = await a_session(store)
    child, capabilities, agents = runtime_for(store, ScriptedProvider([speaks("x")]))
    parent = parent_context(session, capabilities=capabilities)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    sent = await child.send(parent, agent_id, "   ")
    delivered = await child.send(parent, agent_id, "keep going")
    capped = await child.send(parent, agent_id, "x" * 4001)
    assert sent["status"] == "invalid"
    assert delivered["status"] == "delivered"
    assert capped["status"] == "mail-too-long"


async def test_supervisor_start_interrupts_helpers_left_running(store: SessionStore) -> None:
    from lucy_api.stream.emitter import EventEmitter, SqlEventLog
    from lucy_api.turn.supervisor import TurnSupervisor

    class Snapshot:
        async def snapshot(self, session_id: str) -> dict[str, str]:
            return {"session_id": session_id}

    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    events = EventEmitter(SqlEventLog(store), Snapshot())
    supervisor = TurnSupervisor(store, ModelRegistry({}), events, agents=agents)
    await supervisor.start()
    assert (await agents.get(ACCOUNT, agent_id))["status"] == "interrupted"
    assert await agents.interrupt_running() == ()
    await supervisor.aclose()


async def test_a_stranger_cannot_drain_mail(store: SessionStore) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    with pytest.raises(LucyError):
        await agents.drain_mail(STRANGER, agent_id)


async def test_probe_names_how_many_helpers_are_running() -> None:
    pack = AgentsPack()
    work = Registry(now=lambda: datetime.now(UTC))

    async def hang() -> None:
        await asyncio.Event().wait()

    work.start(
        hang(),
        Brief(session_id="ses_x", kind=Kind.helper, role="helper", objective="wait"),
    )
    for _ in range(4):
        await asyncio.sleep(0)
    capabilities = Capabilities((pack,), work=work)
    context = parent_context("ses_x", capabilities=capabilities)
    probed = await pack.probe(context)
    assert "1 helpers running" in probed.detail
    await work.shutdown()


async def test_a_helper_that_raises_is_marked_failed(store: SessionStore) -> None:
    session = await a_session(store)
    child, capabilities, agents = runtime_for(store, ScriptedProvider([speaks("x")]))

    async def boom(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("nope")

    child._loop = boom
    result = await child.run(
        parent_context(session, capabilities=capabilities),
        objective="Go",
        role="helper",
    )

    assert result["status"] == "failed"
    assert "RuntimeError" in result["summary"]
    assert (await agents.get(ACCOUNT, result["agent_id"]))["status"] == "failed"


async def test_a_refused_start_does_not_leave_a_helper_row(store: SessionStore) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    task_id = await agents.add_task(ACCOUNT, session, title="go", agent_id=agent_id)
    await agents.discard_setup(ACCOUNT, session, agent_id, task_id)

    with pytest.raises(LucyError) as caught:
        await agents.get(ACCOUNT, agent_id)
    assert caught.value.code == "not-found"
    assert await agents.tasks(ACCOUNT, session) == ()


async def test_a_cancelled_helper_is_marked_interrupted(store: SessionStore) -> None:
    session = await a_session(store)
    child, capabilities, agents = runtime_for(store, ScriptedProvider([speaks("x")]))
    parent = parent_context(session, capabilities=capabilities)
    agent_id, task_id = await child.prepare(parent, objective="Go", role="helper")
    started = asyncio.Event()

    async def hang(*_args: object, **_kwargs: object) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {}

    child._loop = hang
    task = asyncio.create_task(
        child.run(parent, objective="Go", role="helper", agent_id=agent_id, task_id=task_id)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await agents.get(ACCOUNT, agent_id))["status"] == "interrupted"
    assert (await agents.tasks(ACCOUNT, session))[0].status == "cancelled"


async def test_mail_is_capped_deduped_and_hop_limited(store: SessionStore) -> None:
    from lucy_api.agents.store import MAIL_BURST, MAIL_MAX_CHARS, MAIL_MAX_HOPS

    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    first = await agents.send_mail(ACCOUNT, agent_id, "keep going")
    again = await agents.send_mail(ACCOUNT, agent_id, "keep going")
    assert first == again
    with pytest.raises(LucyError) as far:
        await agents.send_mail(ACCOUNT, agent_id, "loop", hops=MAIL_MAX_HOPS + 1)
    assert far.value.code == "mail-too-far"
    with pytest.raises(LucyError) as long:
        await agents.send_mail(ACCOUNT, agent_id, "x" * (MAIL_MAX_CHARS + 1))
    assert long.value.code == "mail-too-long"
    for index in range(MAIL_BURST - 1):
        await agents.send_mail(ACCOUNT, agent_id, f"steer {index}")
    with pytest.raises(LucyError) as burst:
        await agents.send_mail(ACCOUNT, agent_id, "one more")
    assert burst.value.code == "mail-too-many"


async def test_mail_caps_follow_the_turn_policy_when_they_are_narrower(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    agent_id = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    with pytest.raises(LucyError) as long:
        await agents.send_mail(ACCOUNT, agent_id, "too long", max_chars=3)
    assert long.value.code == "mail-too-long"
    await agents.send_mail(ACCOUNT, agent_id, "one", burst=1)
    with pytest.raises(LucyError) as burst:
        await agents.send_mail(ACCOUNT, agent_id, "two", burst=1)
    assert burst.value.code == "mail-too-many"


async def test_a_helper_that_runs_out_of_time_is_failed_rather_than_cancelled(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    child, capabilities, agents = runtime_for(store, ScriptedProvider([speaks("too late")]))
    parent = parent_context(session, capabilities=capabilities)
    agent_id, task_id = await child.prepare(parent, objective="Go", role="helper")

    async def wait_and_timeout(coro: Any, **_: Any) -> Any:
        task = asyncio.ensure_future(coro)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise TimeoutError

    child._wait = wait_and_timeout  # type: ignore[assignment]
    result = await child.run(
        parent, objective="Go", role="helper", agent_id=agent_id, task_id=task_id
    )
    assert result["status"] == "failed"
    assert "ran out of time" in result["summary"]
    assert (await agents.get(ACCOUNT, agent_id))["status"] == "failed"


async def test_a_journal_claim_is_exclusive_until_the_lease_expires(store: SessionStore) -> None:
    session = await a_session(store)
    agents = AgentStore(store)
    helper = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    task = await agents.add_task(ACCOUNT, session, title="open work", status="pending")
    await agents.claim_task(ACCOUNT, session, task, agent_id=helper)
    other = await agents.insert(ACCOUNT, session, role="helper", objective="also", depth=1)
    with pytest.raises(LucyError) as held:
        await agents.claim_task(ACCOUNT, session, task, agent_id=other)
    assert held.value.code == "conflict"
    await agents.claim_task(ACCOUNT, session, task, agent_id=helper)

    def expire(db: sqlite3.Connection) -> None:
        db.execute("UPDATE journal SET lease_until=0 WHERE id=?", (task,))

    await store.transaction(expire)
    await agents.claim_task(ACCOUNT, session, task, agent_id=other)
    await agents.complete_task(ACCOUNT, session, task)
    with pytest.raises(LucyError):
        await agents.claim_task(ACCOUNT, session, task, agent_id=helper)
    with pytest.raises(LucyError):
        await agents.claim_task(ACCOUNT, session, 99, agent_id=helper)


async def test_reopening_a_finished_helper_keeps_its_transcript(store: SessionStore) -> None:
    session = await a_session(store)
    child, capabilities, _agents = runtime_for(
        store, ScriptedProvider([speaks("first"), speaks("second")])
    )
    parent = parent_context(session, capabilities=capabilities)
    finished = await child.run(parent, objective="Find the date", role="researcher")
    prepared = await child.reopen(parent, finished["agent_id"])
    assert prepared["resume_from"] == finished["agent_id"]
    still = await child.reopen(parent, str(prepared["agent_id"]))
    assert still["status"] == "running"
    continued = await child.run(
        parent,
        objective="Find the date",
        role="researcher",
        agent_id=str(prepared["agent_id"]),
        task_id=int(prepared["task_id"]),
    )
    assert continued["summary"] == "second"
    assert still["status"] == "running"
    missing = await child.reopen(parent, "agt_missing")
    assert missing["status"] == "not-found"


async def test_a_declared_schema_is_parsed_or_named_as_a_miss(store: SessionStore) -> None:
    from lucy_api.agents.types import declared_return

    session = await a_session(store)
    child, capabilities, _agents = runtime_for(store, ScriptedProvider([speaks('{"ok": true}')]))
    parent = parent_context(session, capabilities=capabilities)
    agent_id, task_id = await child.prepare(
        parent, objective="Return json", role="helper", return_schema='{"type":"object"}'
    )
    result = await child.run(
        parent, objective="Return json", role="helper", agent_id=agent_id, task_id=task_id
    )
    assert result["data"] == {"ok": True}
    data, notice = declared_return("not json", '{"type":"object"}')
    assert data is None
    assert "JSON" in notice
    data, notice = declared_return("[1]", '{"type":"object"}')
    assert data is None
    assert "object" in notice
    data, notice = declared_return("{}", "")
    assert data is None
    assert notice == ""


async def test_journal_tools_and_reopen_go_through_the_pack(store: SessionStore) -> None:
    from lucy_api.packs.agents import _journal_claim, _journal_complete, _journal_read, _reopen

    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    capabilities = Capabilities((AgentsPack(),), work=work)
    context = parent_context(session, capabilities=capabilities)
    assert (await _journal_read(context))["status"] == "not_configured"
    assert (await _journal_claim(context, "1"))["status"] == "not_configured"
    assert (await _journal_complete(context, "1"))["status"] == "not_configured"
    assert (await _reopen(work, context, depth=0, agent_id=""))["status"] == "invalid"
    assert (await _reopen(work, context, depth=MAX_DEPTH, agent_id="agt_1"))["status"] == "too_deep"
    assert (await _reopen(work, context, depth=0, agent_id="agt_1"))["status"] == "not_configured"
    child, capabilities, agents = runtime_for(store, ScriptedProvider([speaks("x")]), work=work)
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    helper = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    await agents.finish(ACCOUNT, helper, status="completed", result={"summary": "done"})
    assert (await _reopen(work, context, depth=0, agent_id=helper))["status"] == "at_capacity"
    work_ok = Registry(now=lambda: datetime.now(UTC))
    child_ok, capabilities_ok, agents_ok = runtime_for(
        store, ScriptedProvider([speaks("next")]), work=work_ok
    )
    context_ok = parent_context(session, capabilities=capabilities_ok)
    context_ok.child = child_ok
    helper_ok = await agents_ok.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    task = await agents_ok.add_task(ACCOUNT, session, title="open", status="pending")
    claimed = await _journal_claim(context_ok, str(task))
    assert claimed["status"] == "claimed"
    listed = await _journal_read(context_ok)
    assert listed["tasks"][0]["id"] == str(task)
    assert (await _journal_claim(context_ok, "nope"))["status"] == "invalid"
    assert (await _journal_complete(context_ok, "nope"))["status"] == "invalid"
    assert (await _journal_complete(context_ok, str(task)))["status"] == "completed"
    assert (await _journal_claim(context_ok, ""))["status"] == "invalid"
    assert (await _journal_complete(context_ok, ""))["status"] == "invalid"
    missing = await _reopen(work_ok, context_ok, depth=0, agent_id="agt_nope")
    assert missing["status"] == "not-found"
    await agents_ok.finish(ACCOUNT, helper_ok, status="completed", result={"summary": "done"})
    reopened = await _reopen(work_ok, context_ok, depth=0, agent_id=helper_ok)
    assert reopened["state"] == "running"
    fetched = await work_ok.wait(str(reopened["id"]), 30)
    payload = fetched.payload
    assert isinstance(payload, dict)
    await work_ok.shutdown()


async def test_pack_operations_dispatch_reopen_and_journal(store: SessionStore) -> None:
    from types import SimpleNamespace

    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC))
    child, capabilities, agents = runtime_for(store, ScriptedProvider([speaks("next")]), work=work)
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    pack = next(pack for pack in capabilities.packs if pack.id == "agents")
    ops = {operation.name: operation for operation in pack.operations(context)}
    listed = await ops["journal.read"].run(SimpleNamespace(input={}, ctx=context))
    assert listed["tasks"] == []
    helper = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    task = await agents.add_task(ACCOUNT, session, title="open", status="pending")
    claimed = await ops["journal.claim"].run(SimpleNamespace(input={"id": str(task)}, ctx=context))
    assert claimed["status"] == "claimed"
    finished = await ops["journal.complete"].run(
        SimpleNamespace(input={"id": str(task)}, ctx=context)
    )
    assert finished["status"] == "completed"
    await agents.finish(ACCOUNT, helper, status="completed", result="plain text")
    reopened = await ops["agents.reopen"].run(
        SimpleNamespace(input={"id": helper, "return_schema": '{"type":"object"}'}, ctx=context)
    )
    assert reopened["state"] == "running"
    fetched = await work.wait(str(reopened["id"]), 30)
    assert isinstance(fetched.payload, dict)
    await work.shutdown()


async def test_runtime_journal_misses_and_non_object_delegation_are_named(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    child, capabilities, agents = runtime_for(
        store, ScriptedProvider([speaks("ok"), speaks("not json")])
    )
    parent = parent_context(session, capabilities=capabilities)
    parent.agent_id = "agt_parent"
    helper = await agents.insert(ACCOUNT, session, role="helper", objective="go", depth=1)
    claimed = await child.claim(parent, "99")
    assert claimed["status"] == "not-found"
    completed = await child.complete(parent, "99")
    assert completed["status"] == "not-found"

    def scramble(db: sqlite3.Connection) -> None:
        db.execute("UPDATE agents SET delegation_json='[]' WHERE id=?", (helper,))

    await store.transaction(scramble)
    prepared_id, task_id = (
        helper,
        await agents.add_task(ACCOUNT, session, title="go", agent_id=helper),
    )
    result = await child.run(
        parent, objective="Go", role="helper", agent_id=prepared_id, task_id=task_id
    )
    assert result["status"] == "ok"

    schema_id, schema_task = await child.prepare(
        parent, objective="Return json", role="helper", return_schema='{"type":"object"}'
    )
    missed = await child.run(
        parent,
        objective="Return json",
        role="helper",
        agent_id=schema_id,
        task_id=schema_task,
    )
    assert missed["data"] is None
    assert "JSON" in missed["notice"]

    finished = await agents.insert(ACCOUNT, session, role="helper", objective="done", depth=1)
    await agents.finish(ACCOUNT, finished, status="completed", result={"summary": ""})
    reopened = await child.reopen(parent, finished)
    assert reopened["resume_from"] == finished
    await child.discard_setup(parent, str(reopened["agent_id"]), int(reopened["task_id"]))


def test_the_brief_names_a_declared_schema_and_a_reopened_predecessor() -> None:
    text = _brief_text(
        Delegation(
            objective="Check the dates",
            role="reviewer",
            return_schema='{"type":"object"}',
            resume_from="agt_old",
        )
    )
    assert "JSON" in text
    assert "agt_old" in text
