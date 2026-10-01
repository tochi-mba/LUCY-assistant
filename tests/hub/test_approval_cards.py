"""One card per permission per plan: the person answers a team once, and only for that team.

A plan that started five helpers in `ask` mode put five approval cards in front of the
person, each asking "Start a helper?" about one call. The calls a plan parks under one
permission are now one card that names the permission once and counts the calls. What that
must not become is a wider answer than it was: a yes, once, runs exactly the calls on the card,
each by its own arguments, and nothing in a later plan. Calls under another permission are
another card.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from conftest import ACCOUNT
from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.clients.testing import FakeHttp
from lucy_api.context.build import Live
from lucy_api.context.types import Trust
from lucy_api.evals.transcript import exchange_for, pending_approvals
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.base import Availability, Permission, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.approvals import (
    ApprovedCall,
    Ask,
    AskedCall,
    answer_approval,
    approved_calls,
    card_sentence,
    cards,
    mark_executed,
    open_approval,
    reopen,
)
from lucy_api.permissions.gate import once_key
from lucy_api.permissions.live import PendingLive
from lucy_api.permissions.replay import needs
from lucy_api.permissions.store import calls_of, grants_for
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.readable import readable
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor
from lucy_api.work import Registry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path


class Crew:
    """Hiring under one permission, counted by role; paying under another."""

    id = "crew"
    title = "Crew"
    summary = "Hire and pay a crew."

    def __init__(self) -> None:
        self.hired: list[str] = []
        self.paid: list[str] = []

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Permission, ...]:
        return (
            Permission(
                id="crew.hire",
                title="Hire someone",
                description="Hires.",
                risk="write",
                covers=("crew.hire",),
                tally="role",
            ),
            Permission(
                id="crew.pay",
                title="Pay someone",
                description="Pays.",
                risk="write",
                covers=("crew.pay",),
            ),
        )

    def result_trust(self, operation: str, data: object) -> Trust:
        del operation, data
        return Trust.observed

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready)

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def hire(run: Any) -> dict[str, str]:
            self.hired.append(f"{run.input['role']}:{run.input['name']}")
            return {"hired": str(run.input["name"])}

        async def pay(run: Any) -> dict[str, str]:
            self.paid.append(str(run.input["name"]))
            return {"paid": str(run.input["name"])}

        return (
            define_operation(
                {
                    "name": "crew.hire",
                    "description": "Hire one person.",
                    "input": object_schema({"role": string_schema(), "name": string_schema()}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": hire,
                }
            ),
            define_operation(
                {
                    "name": "crew.pay",
                    "description": "Pay one person.",
                    "input": object_schema({"name": string_schema()}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": pay,
                }
            ),
        )


def hire(step: str, role: str, name: str) -> dict[str, Any]:
    return {"id": step, "op": "crew.hire", "input": {"role": role, "name": name}}


TEAM = {
    "steps": [
        hire("a", "researcher", "Ada"),
        hire("b", "reviewer", "Bo"),
        hire("c", "researcher", "Cy"),
        hire("d", "reviewer", "Di"),
        hire("e", "reviewer", "Ed"),
        {"id": "p", "op": "crew.pay", "input": {"name": "Ada"}},
    ]
}


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


class Conversation:
    """One conversation with the crew, a scripted model, and a supervisor to run its turns."""

    def __init__(self, store: SessionStore, script: list[Any]) -> None:
        self.store = store
        self.crew = Crew()
        self.capabilities = Capabilities((HelpPack(), self.crew))
        self.provider = ScriptedProvider(script)
        self.supervisor = TurnSupervisor(
            store,
            ModelRegistry({"scripted": lambda _: self.provider}),
            EventEmitter(SqlEventLog(store), _Snapshot()),
            capabilities=self.capabilities,
        )
        self.session = ""

    async def say(self, text: str, key: str) -> str:
        if not self.session:
            created = await self.store.create(ACCOUNT, CreateSession(model="scripted:demo"), "k")
            self.session = str(created["id"])
        queued = await submit_messages(
            self.store, ACCOUNT, self.session, [{"type": "input.message", "content": text}], key
        )
        await self.run(str(queued["id"]))
        return str(queued["id"])

    async def run(self, turn: str) -> None:
        scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=self.session)
        self.supervisor.authorize(
            turn,
            PreparedTurn(
                pack_context=self.capabilities.context_for(scope, http=FakeHttp()), live=Live()
            ),
        )
        self.supervisor.wake()
        await self.supervisor.join()

    async def cards(self) -> list[dict[str, Any]]:
        items = await self.store.records(ACCOUNT, self.session, "items")
        return [item["content"] for item in items if item["type"] == "approval_request"]

    async def answer(
        self, card: Mapping[str, Any], turn: str, *, approved: bool, **extra: Any
    ) -> None:
        await answer_approval(
            self.store,
            ACCOUNT,
            self.session,
            {
                "type": "input.approval",
                "approval_id": card["approval_id"],
                "approved": approved,
                **extra,
            },
            f"answer-{card['approval_id']}",
        )
        await self.run(turn)


async def test_a_plan_s_calls_under_one_permission_are_one_card_counted_by_role(
    store: SessionStore,
) -> None:
    talk = Conversation(store, [plans(TEAM)])
    await talk.say("Hire the team and pay Ada.", "q1")

    hiring, paying = await talk.cards()
    assert hiring["permission"] == "crew.hire"
    assert hiring["tool"] == "crew.hire"
    assert hiring["description"] == (
        "Hire someone, 5 calls in this plan: researcher x2, reviewer x3"
    )
    assert hiring["count"] == 5
    assert hiring["arguments"] == {}
    assert [(step["step"], step["arguments"]["name"]) for step in hiring["steps"]] == [
        ("a", "Ada"),
        ("b", "Bo"),
        ("c", "Cy"),
        ("d", "Di"),
        ("e", "Ed"),
    ]
    assert hiring["limit"] == {"field": "role", "values": ["researcher", "reviewer"]}, (
        "a card counted by a field offers that field, once per value, for a limited yes"
    )
    assert paying["permission"] == "crew.pay", "another permission is another card"
    assert "steps" not in paying
    assert paying["arguments"] == {"name": "Ada"}
    assert "limit" not in paying, "a permission without a tally offers no limit"
    events = await store.records(ACCOUNT, talk.session, "events")
    requested = [row["data"] for row in events if row["type"] == "lucy.approval.requested"]
    assert [row["count"] for row in requested if "count" in row] == [5]
    await talk.supervisor.aclose()


async def test_one_yes_runs_exactly_the_calls_on_the_card_and_the_other_card_decides_its_own(
    store: SessionStore,
) -> None:
    talk = Conversation(store, [plans(TEAM), speaks("Hired five; paying was refused.")])
    turn = await talk.say("Hire the team and pay Ada.", "q1")
    hiring, paying = await talk.cards()

    await talk.answer(paying, turn, approved=False, instruction="not yet")
    await talk.answer(hiring, turn, approved=True)

    assert talk.crew.hired == [
        "researcher:Ada",
        "reviewer:Bo",
        "researcher:Cy",
        "reviewer:Di",
        "reviewer:Ed",
    ]
    assert talk.crew.paid == []
    rows = await store.records(ACCOUNT, talk.session, "items")
    ran = [row["content"]["step_id"] for row in rows if row["type"] == "tool_result"]
    assert ran == ["a", "b", "c", "d", "e"]
    await talk.supervisor.aclose()


async def test_a_card_s_yes_covers_no_call_it_did_not_show_and_no_later_plan(
    store: SessionStore,
) -> None:
    """The one thing a card of several must never be: a wider answer than the calls on it."""
    two = {"steps": [hire("a", "researcher", "Ada"), hire("b", "reviewer", "Bo")]}
    again = {"steps": [hire("a", "researcher", "Ada"), hire("z", "reviewer", "Zed")]}
    talk = Conversation(store, [plans(two), plans(again)])
    turn = await talk.say("Hire two.", "q1")
    [card] = await talk.cards()
    await talk.answer(card, turn, approved=True)

    assert talk.crew.hired == ["researcher:Ada", "reviewer:Bo"]
    later = await talk.cards()
    assert len(later) == 2, "the later plan is asked about again, the same call included"
    assert [step["arguments"]["name"] for step in later[1]["steps"]] == ["Ada", "Zed"]
    await talk.supervisor.aclose()


async def test_one_no_refuses_every_call_on_the_card(store: SessionStore) -> None:
    two = {"steps": [hire("a", "researcher", "Ada"), hire("b", "reviewer", "Bo")]}
    talk = Conversation(store, [plans(two), speaks("Not hiring, as you asked.")])
    turn = await talk.say("Hire two.", "q1")
    [card] = await talk.cards()
    await talk.answer(card, turn, approved=False, instruction="hire nobody")

    assert talk.crew.hired == []
    grants = await grants_for(store, ACCOUNT, "personal", session_id=talk.session, turn_id=turn)
    keys = [once_key("crew.hire", step["input"]) for step in two["steps"]]
    assert [grants[key].decision for key in keys] == ["deny", "deny"]
    assert {grants[key].instruction for key in keys} == {"hire nobody"}
    await talk.supervisor.aclose()


async def test_a_card_answered_for_the_session_lets_the_next_team_through(
    store: SessionStore,
) -> None:
    two = {"steps": [hire("a", "researcher", "Ada"), hire("b", "reviewer", "Bo")]}
    three = {"steps": [hire("c", "skeptic", "Cy"), hire("d", "skeptic", "Di")]}
    talk = Conversation(store, [plans(two), speaks("Hired."), plans(three), speaks("Hired.")])
    turn = await talk.say("Hire two.", "q1")
    [card] = await talk.cards()
    await talk.answer(card, turn, approved=True, lifetime="session")
    await talk.say("Now two skeptics.", "q2")

    assert talk.crew.hired[-2:] == ["skeptic:Cy", "skeptic:Di"]
    assert len(await talk.cards()) == 1, "the standing answer covered the next team"
    await talk.supervisor.aclose()


# --- the pieces ----------------------------------------------------------------------------


def test_cards_group_by_permission_in_the_order_first_asked() -> None:
    asks = [
        {"permission": "p", "step": "1"},
        {"permission": "q", "step": "2"},
        {"permission": "p", "step": "3"},
    ]
    assert [[ask["step"] for ask in card] for card in cards(asks)] == [["1", "3"], ["2"]]


def test_a_card_without_a_tally_counts_its_calls_and_names_none() -> None:
    asks = [
        {"permission": "p", "title": "Write a file", "label": ""},
        {"permission": "p", "title": "Write a file", "label": ""},
    ]
    assert card_sentence(asks) == "Write a file, 2 calls in this plan"
    assert card_sentence([{"operation": "x.y"}, {}]) == "x.y, 2 calls in this plan"


def test_calls_of_a_card_skip_anything_not_in_the_shape_it_was_written() -> None:
    payload = {
        "calls": [
            {"operation": "crew.hire", "arguments": {"name": "Ada"}},
            {"operation": "crew.hire", "arguments": "broken"},
            "junk",
            {"arguments": {"name": "Bo"}},
        ]
    }
    found = calls_of("crew.hire", payload)
    assert [(op, args) for op, args, _entry in found] == [
        ("crew.hire", {"name": "Ada"}),
        ("crew.hire", {"name": "Bo"}),
    ]
    assert calls_of("crew.pay", {"arguments": "no"})[0][:2] == ("crew.pay", {})


async def _card(store: SessionStore, plan: dict[str, Any]) -> tuple[str, str, str]:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "k")
    session = str(created["id"])
    queued = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "q"
    )
    turn = str(queued["id"])
    await store.transaction(
        lambda db: db.execute("UPDATE turns SET status='running' WHERE id=?", (turn,))
    )
    parked = tuple(step["id"] for step in plan["steps"])
    approval = await open_approval(
        store,
        account=ACCOUNT,
        session_id=session,
        turn_id=turn,
        ask=Ask(
            permission="crew.hire",
            operation="crew.hire",
            description="Hire someone, 2 calls in this plan",
            calls=tuple(
                AskedCall(
                    operation=step["op"],
                    arguments=step["input"],
                    description=f"hire {step['input']['name']}",
                    needs=needs(plan, step["id"], parked=parked),
                )
                for step in plan["steps"]
            ),
        ),
    )
    return session, turn, approval


async def test_a_card_s_calls_are_replayed_each_by_its_own_step(store: SessionStore) -> None:
    plan = {"steps": [hire("a", "researcher", "Ada"), hire("b", "reviewer", "Bo")]}
    session, turn, approval = await _card(store, plan)
    await answer_approval(
        store,
        ACCOUNT,
        session,
        {"type": "input.approval", "approval_id": approval, "approved": True},
        "yes",
    )

    calls = await approved_calls(store, turn)
    assert [(call.approval_id, call.step, call.arguments["name"]) for call in calls] == [
        (approval, "a", "Ada"),
        (approval, "b", "Bo"),
    ]
    assert [[step["id"] for step in call.needs] for call in calls] == [["a"], ["b"]]

    await mark_executed(store, calls)
    assert await approved_calls(store, turn) == ()
    moved = {"steps": [hire("b", "reviewer", "Bo"), hire("x", "x", "X"), hire("a", "r", "Ada")]}
    await reopen(store, calls, moved, parked=("a", "b", "x"))
    back = await approved_calls(store, turn)
    assert [call.step for call in back] == ["a", "b"]
    assert [call.plan for call in back] == [call.plan for call in back[:1]] * 2
    assert back[0].plan != calls[0].plan, "recorded again against the plan that parked"


async def test_reopening_leaves_a_card_entry_it_was_not_given_as_it_was(
    store: SessionStore,
) -> None:
    plan = {"steps": [hire("a", "researcher", "Ada"), hire("b", "reviewer", "Bo")]}
    _session, turn, approval = await _card(store, plan)

    def odd(db: sqlite3.Connection) -> None:
        row = db.execute("SELECT input_json FROM approvals WHERE id=?", (approval,)).fetchone()
        payload = json.loads(row[0])
        payload["calls"].append("junk")
        db.execute("UPDATE approvals SET input_json=? WHERE id=?", (json.dumps(payload), approval))

    await store.transaction(odd)
    only_a = ApprovedCall(approval_id=approval, operation="crew.hire", arguments={}, step="a")
    await reopen(store, (only_a,), {"steps": [hire("a", "r", "Ada")]}, parked=("a",))

    stored = await store.transaction(
        lambda db: db.execute("SELECT input_json FROM approvals WHERE id=?", (approval,)).fetchone()
    )
    entries = json.loads(stored[0])["calls"]
    assert entries[1]["plan"] == needs(plan, "b").plan, "b was not given, so b is unchanged"
    assert entries[0]["plan"] != entries[1]["plan"]
    assert entries[2] == "junk"
    assert turn


async def test_a_card_reads_to_the_model_and_to_the_live_block_as_one_ask(
    store: SessionStore,
) -> None:
    plan = {"steps": [hire("a", "researcher", "Ada"), hire("b", "reviewer", "Bo")]}
    session, turn, _approval = await _card(store, plan)
    items = await store.records(ACCOUNT, session, "items")
    [card] = [item for item in items if item["type"] == "approval_request"]

    assert readable("approval_request", card["content"]) == (
        "[asked the person to approve, on one card, Hire someone, 2 calls in this plan: "
        'crew.hire(name="Ada", role="researcher"); crew.hire(name="Bo", role="reviewer")]'
    )
    pending = await PendingLive(store).fetch(session)
    assert pending.approvals == ("crew.hire -- Hire someone, 2 calls in this plan",)
    asks = exchange_for(items, turn).asks
    assert [ask.arguments for ask in asks] == [
        "name=Ada, role=researcher",
        "name=Bo, role=reviewer",
    ]
    assert len({ask.approval_id for ask in asks}) == 1
    assert pending_approvals(items, turn) == (asks[0].approval_id,), "one card, one answer"


async def test_starting_a_team_of_helpers_asks_once_counted_by_role() -> None:
    """The case that asked for this: helpers are counted by the role each was given."""
    capabilities = Capabilities((HelpPack(), AgentsPack()), work=Registry(now=datetime.now))
    context = capabilities.context_for(
        SessionScope(account_id=ACCOUNT, profile="personal", session_id="ses_team")
    )
    team = {
        "steps": [
            {"id": f"s{n}", "op": "agents.spawn", "input": {"objective": "Look", "role": role}}
            for n, role in enumerate(("researcher", "reviewer", "reviewer"))
        ]
    }
    parked = await capabilities.execute(team, context)

    issues = parked["issues"]
    assert {issue["permission"] for issue in issues} == {"agents.delegate"}
    assert card_sentence(issues) == (
        "Start a helper, 3 calls in this plan: researcher x1, reviewer x2"
    )
    assert [len(card) for card in cards(issues)] == [3]
