"""A team of helpers: more than the cap started in one plan, groups told as one.

A plan that starts a team larger than the person's cap used to have every spawn past it
refused, and the model was left counting slots and starting the rest one at a time. A spawn
past the cap is now queued and started by the hub as slots free, never more at once than
the person allows. A team started under one group name is told once, when its last member
ends, rather than once a member -- which, for helpers that wake the session, was a turn a
member.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.model.types import Reply
from lucy_api.packs.agents import AgentsPack, _list, _reopen, _spawn
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.packs.work import WorkPack, _check
from lucy_api.permissions.gate import Grant
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.stream.events import WORK_GROUP_FINISHED
from lucy_api.work import Registry, State, Waker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from lucy_api.model.types import Request
    from lucy_api.packs.context import PackContext

ACCOUNT = "acct_teams"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    return str(created["id"])


def runtime_for(
    store: SessionStore, provider: ScriptedProvider, *, work: Registry | None = None
) -> tuple[ChildRuntime, Capabilities, AgentStore]:
    capabilities = Capabilities((HelpPack(), WorkPack(), AgentsPack()), work=work)
    agents = AgentStore(store)
    child = ChildRuntime(
        store, agents, ModelRegistry({"scripted": lambda _model: provider}), capabilities
    )
    capabilities.child = child
    return child, capabilities, agents


def parent_context(session_id: str, *, capabilities: Capabilities) -> PackContext:
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=session_id)
    return capabilities.context_for(scope)


# --------------------------------------------------------------------------------------
# Queued past the cap
# --------------------------------------------------------------------------------------


async def test_spawn_past_the_cap_queues_and_starts_when_a_helper_finishes(
    store: SessionStore,
) -> None:
    """The cap is the person's; the model is handed a handle, not a refusal to retry."""
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC))
    gate = asyncio.Event()

    class Held(ScriptedProvider):
        async def complete(self, request: Request) -> Reply:
            await gate.wait()
            return await super().complete(request)

    child, capabilities, agents = runtime_for(
        store, Held([speaks("first"), speaks("second")]), work=work
    )
    context = parent_context(session, capabilities=capabilities)
    context.policy = TurnPolicy(agent_max_concurrent=1)
    first = await _spawn(work, context, depth=0, objective="First look", role="one")
    second = await _spawn(work, context, depth=0, objective="Second look", role="two")

    assert first["state"] == "running"
    assert second["state"] == "queued"
    assert "1 helpers are running" in second["advice"]
    assert "work.cancel takes it out of the queue" in second["advice"]
    await asyncio.sleep(0)
    row = await agents.get(ACCOUNT, str(second["id"]))
    assert row["status"] == "queued"
    assert row["started_at"] is None
    listed = await _list(work, context)
    assert [line["state"] for line in listed["running"]] == ["running", "queued"]
    assert listed["queued"] == 1
    probed = await AgentsPack().probe(context)
    assert probed.detail == "1 helpers running, 1 queued"

    gate.set()
    done = await work.wait(str(second["id"]), 30)
    assert isinstance(done.payload, dict)
    assert done.payload["summary"] == "second"
    finished = await agents.get(ACCOUNT, str(second["id"]))
    assert finished["status"] == "completed"
    assert finished["started_at"] is not None
    assert "queued" not in await _list(work, context)
    assert child is context.child


async def test_a_queued_helper_cancelled_before_it_starts_is_recorded_cancelled(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    child, capabilities, agents = runtime_for(store, ScriptedProvider([]), work=work)
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    queued = await _spawn(work, context, depth=0, objective="Never mind", role="late")

    record = work.cancel(str(queued["id"]))
    for _ in range(20):
        await asyncio.sleep(0)

    assert record.state.value == "cancelled"
    row = await agents.get(ACCOUNT, str(queued["id"]))
    assert row["status"] == "interrupted"
    assert row["interrupted_reason"] == "cancelled"
    assert (await agents.tasks(ACCOUNT, session))[0].status == "cancelled"
    assert await child.stopped(context) == [], "cancelling it was the point"


async def test_a_queued_helper_takes_mail_and_is_not_reopened(store: SessionStore) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    child, capabilities, _agents = runtime_for(store, ScriptedProvider([]), work=work)
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    queued = await _spawn(work, context, depth=0, objective="Wait", role="late")

    sent = await child.send(context, str(queued["id"]), "look at the tests first")
    reopened = await child.reopen(context, str(queued["id"]))

    assert sent["status"] == "delivered"
    assert reopened["status"] == "queued"
    assert "still queued" in reopened["message"]
    work.cancel(str(queued["id"]))


async def test_reopen_queues_past_the_cap_too(store: SessionStore) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC))
    child, capabilities, _agents = runtime_for(store, ScriptedProvider([speaks("x")]), work=work)
    context = parent_context(session, capabilities=capabilities)
    done = await child.run(context, objective="One pass", role="one")
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    context.policy = TurnPolicy(agent_max_concurrent=1)

    again = await _reopen(work, context, depth=0, agent_id=str(done["agent_id"]))
    refused = await _reopen(work, context, depth=0, agent_id=str(done["agent_id"]))

    assert again["state"] == "queued"
    assert again["resume_from"] == done["agent_id"]
    assert refused["status"] == "continued"
    work.cancel(str(again["id"]))


async def test_reopen_past_a_full_queue_is_refused_and_discards_its_setup(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    occupied = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    child, capabilities, _agents = runtime_for(
        store, ScriptedProvider([speaks("x")]), work=occupied
    )
    context = parent_context(session, capabilities=capabilities)
    context.child = child
    context.policy = TurnPolicy(agent_max_concurrent=1)
    await _spawn(occupied, context, depth=0, objective="fills the queue", role="one")

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


# --------------------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------------------


async def test_a_helper_started_in_a_group_is_listed_with_it_and_its_brief_keeps_it(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    child, capabilities, agents = runtime_for(store, ScriptedProvider([]), work=work)
    context = parent_context(session, capabilities=capabilities)
    context.child = child

    started = await _spawn(
        work,
        context,
        depth=0,
        objective="Read it as a protocol",
        role="protocol",
        group="  reviewers  ",
    )
    alone = await _spawn(work, context, depth=0, objective="Alone", role="solo")

    assert started["group"] == "reviewers"
    assert "group" not in alone
    listed = await _list(work, context)
    assert [line.get("group") for line in listed["running"]] == ["reviewers", None]
    row = await agents.get(ACCOUNT, str(started["id"]))
    assert row["delegation"]["group"] == "reviewers"
    assert "group" not in (await agents.get(ACCOUNT, str(alone["id"])))["delegation"]
    [shown, _other] = work.snapshot(session)
    assert shown.group == "reviewers"
    work.cancel(str(started["id"]))
    work.cancel(str(alone["id"]))


async def test_work_check_names_a_group_once_its_last_member_has_ended(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC))
    child, capabilities, _agents = runtime_for(
        store, ScriptedProvider([speaks("one"), speaks("two")]), work=work
    )
    context = parent_context(session, capabilities=capabilities)
    first = await _spawn(work, context, depth=0, objective="First", role="a", group="pair")
    await work.wait(str(first["id"]), 30)
    checked = _check(work, session)
    assert [notice["group"] for notice in checked["finished"]] == ["pair"]
    assert checked["groups"][0]["group"] == "pair"
    assert checked["groups"][0]["ids"] == [first["id"]]
    assert checked["groups"][0]["line"].startswith("group pair (1 member) has ended")
    assert "groups" not in _check(work, session)
    assert child is context.child


# --------------------------------------------------------------------------------------
# The whole team, end to end
# --------------------------------------------------------------------------------------

FINDINGS = json.dumps({"findings": [{"claim": "the retry loop never backs off"}]})
VERDICT = json.dumps({"verdict": "confirmed", "why": "line 40 retries at once"})


class Team(ScriptedProvider):
    """One model behind every helper, holding each one's answer until the test lets it go.

    Keyed by the helper's objective, which its brief carries, so the test decides who
    finishes when; and it counts how many helpers are asking at once, which is the one
    number that proves the cap.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.gates: dict[str, asyncio.Event] = {}
        self.asking = 0
        self.most = 0
        self.asked: list[str] = []
        self.full = asyncio.Event()
        self.heard = asyncio.Condition()

    async def until_asked(self, count: int) -> None:
        """Until this many helpers have asked, however long their starting takes."""
        async with self.heard:
            await asyncio.wait_for(self.heard.wait_for(lambda: len(self.asked) >= count), 5)

    def gate(self, objective: str) -> asyncio.Event:
        return self.gates.setdefault(objective, asyncio.Event())

    async def complete(self, request: Request) -> Reply:
        brief = next(m.content for m in request.messages if "Objective: " in str(m.content))
        objective = str(brief).split("Objective: ", 1)[1].split("\n", 1)[0]
        async with self.heard:
            self.asked.append(objective)
            self.heard.notify_all()
        self.asking += 1
        self.most = max(self.most, self.asking)
        if self.asking == TurnPolicy().agent_max_concurrent:
            self.full.set()
        try:
            await self.gate(objective).wait()
        finally:
            self.asking -= 1
        answer = VERDICT if objective.startswith("Refute") else FINDINGS
        return Reply(text=answer)


def spawn(index: int, objective: str, role: str, group: str) -> dict[str, Any]:
    return {
        "id": f"{role}{index}",
        "op": "agents.spawn",
        "input": {
            "objective": objective,
            "role": role,
            "group": group,
            "return_schema": '{"type": "object"}',
        },
    }


class Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


async def test_one_plan_runs_two_groups_at_once_and_skeptics_queue_behind_them(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    work = Registry(now=lambda: datetime.now(UTC))
    waker = Waker(store, EventEmitter(SqlEventLog(store), Snapshot()))
    told: list[str] = []
    all_told = asyncio.Event()

    async def team_told(team: Any) -> None:
        await waker.on_team_finished(team)
        told.append(team.group)
        if len(told) == 3:
            all_told.set()

    work.on_finished(waker.on_finished)
    work.on_team_finished(team_told)
    model = Team()
    _child, capabilities, agents = runtime_for(store, model, work=work)
    context = parent_context(session, capabilities=capabilities)
    # One answer for the permission covers every helper the plan starts.
    context.grants = {
        "agents.delegate": Grant(permission="agents.delegate", decision="allow", profile="*")
    }
    assert context.policy.agent_max_concurrent == 5, "the person's default cap"

    researchers = ["Research how peers retry", "Research what the spec requires"]
    reviewers = ["Review the protocol", "Review the platform", "Review the onboarding"]
    team_plan = {
        "steps": [
            *(spawn(n, text, "researcher", "researchers") for n, text in enumerate(researchers)),
            *(spawn(n, text, "reviewer", "reviewers") for n, text in enumerate(reviewers)),
        ]
    }
    ran = await capabilities.execute(team_plan, context)
    assert [step["status"] for step in ran["steps"]] == ["ok"] * 5
    assert {step["data"]["state"] for step in ran["steps"]} == {"running"}
    await asyncio.wait_for(model.full.wait(), 5)
    assert sorted(model.asked) == sorted(researchers + reviewers), "all five at once"

    skeptics = ["Refute the retry finding", "Refute the onboarding finding"]
    skeptic_plan = {
        "steps": [spawn(n, text, "skeptic", "skeptics") for n, text in enumerate(skeptics)]
    }
    queued = await capabilities.execute(skeptic_plan, context)
    handles = [str(step["data"]["id"]) for step in queued["steps"]]
    assert [step["data"]["state"] for step in queued["steps"]] == ["queued", "queued"]
    assert [work.state_of(handle) for handle in handles] == [State.queued, State.queued]
    rows = [await agents.get(ACCOUNT, handle) for handle in handles]
    assert [row["status"] for row in rows] == ["queued", "queued"]

    for objective in researchers:
        model.gate(objective).set()
    await model.until_asked(7)
    assert sorted(model.asked[5:]) == sorted(skeptics), "each starts as a slot frees"
    assert [work.state_of(handle) for handle in handles] == [State.running, State.running]
    assert model.most == 5, "never past the person's cap"

    for objective in reviewers + skeptics:
        model.gate(objective).set()
    await asyncio.wait_for(all_told.wait(), 5)
    await work.shutdown()

    events = await store.records(ACCOUNT, session, "events")
    ended = [row["data"] for row in events if row["type"] == WORK_GROUP_FINISHED]
    assert [group["group"] for group in ended] == ["researchers", "reviewers", "skeptics"]
    assert [len(group["members"]) for group in ended] == [2, 3, 2]
    assert all(m["state"] == "succeeded" for group in ended for m in group["members"])
    turns = await store.records(ACCOUNT, session, "turns")
    assert len(turns) == 1, "one wake for the first group; the others wait behind it"
    items = await store.records(ACCOUNT, session, "items")
    [item] = [row for row in items if not row.get("agent_id")]
    assert str(item["content"]).startswith("[harness: group researchers (2 members) has ended")
    groups = _check(work, session)["groups"]
    assert [group["group"] for group in groups] == ["researchers", "reviewers", "skeptics"]
    verdict = work.result(handles[0]).payload
    assert isinstance(verdict, dict)
    assert verdict["data"] == json.loads(VERDICT)
