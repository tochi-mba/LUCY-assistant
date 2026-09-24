"""Binding a held-back capability, and what recency means.

Deferral keeps the plan schema short by holding back the least recently used capabilities,
and `capabilities.use` is the way back. Two things were wrong with the way back.

**It said "next turn" about something that takes effect next plan.** The schema and the
executor are rebuilt from recency every round, so a capability bound in one plan is callable
in the very next -- but the tool result said "will be available next turn", and the prompt's
capability list was only rebuilt when a session setting changed. On a fresh session the
workspace is held back (it sorts after `watch`), so "build me a calculator website" bound the
workspace and then stopped. On the weakest model it went further, and told the person "Done.
Your calculator website is ready in the `calculator` folder" having written nothing at all.

**Recency meant "first used first".** `remember_use` appended and never moved, and every plan
re-marked every bound capability as used. Once four capabilities had been used in a session,
`capabilities.use` on a fifth answered `bound: true` and never bound it: the one-way door
`ALWAYS` exists to prevent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from conftest import ACCOUNT
from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.base import Availability, State
from lucy_api.packs.help import HelpPack, _use
from lucy_api.packs.registry import DEFER_ABOVE, KEEP_RECENT
from lucy_api.packs.service import Capabilities, _packs_run
from lucy_api.prompt.sections import PromptContext, _capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from lucy_api.model.types import Request
    from lucy_api.packs.context import PackContext

SESSION = "ses_a"


class Gadget:
    """A ready capability with one read operation, `<id>.ping`."""

    def __init__(self, pack_id: str) -> None:
        self.id = pack_id
        self.title = pack_id.title()
        self.summary = f"The {pack_id} capability."

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Any, ...]:
        return ()

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready, detail="connected")

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def run(_run: object) -> dict[str, bool]:
            return {"ok": True}

        return (
            define_operation(
                {
                    "name": f"{self.id}.ping",
                    "description": "Ping.",
                    "input": object_schema({}),
                    "output": value(object_schema({"ok": string_schema()})),
                    "effects": "read",
                    "run": run,
                }
            ),
        )


def _run(context: object, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(ctx=context, input=payload)


def _crowded() -> tuple[Capabilities, PackContext]:
    """More ready capabilities than deferral allows, so some are held back.

    `help` is always bound; the gadgets compete for the `KEEP_RECENT` slots and, with no
    recency yet, are kept in id order -- so the last ones are the held-back ones.
    """
    gadgets = tuple(Gadget(f"g{index}") for index in range(DEFER_ABOVE + 1))
    capabilities = Capabilities((HelpPack(), *gadgets))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id=SESSION)
    )
    return capabilities, context


def _bound(capabilities: Capabilities, context: PackContext) -> set[str]:
    assert context.catalogue is not None
    bound, _deferred = capabilities.bound_for(context.catalogue, SESSION)
    return {item.pack.id for item in bound}


def _use_plan(pack_id: str) -> dict[str, object]:
    return {"steps": [{"id": "bind", "op": "capabilities.use", "input": {"id": pack_id}}]}


def _ping_plan(*pack_ids: str) -> dict[str, object]:
    return {
        "steps": [
            {"id": f"ping_{pack_id}", "op": f"{pack_id}.ping", "input": {}} for pack_id in pack_ids
        ]
    }


async def test_a_capability_bound_in_one_plan_is_callable_in_the_very_next_one() -> None:
    capabilities, context = _crowded()
    await capabilities.probe(context)
    held_back = f"g{DEFER_ABOVE}"
    assert held_back not in _bound(capabilities, context)

    await capabilities.execute(_use_plan(held_back), context)
    assert context.catalogue is not None
    schema = capabilities.plan_schema(context.catalogue, SESSION, context)
    result = await capabilities.execute(_ping_plan(held_back), context)

    assert f"{held_back}.ping" in str(schema)
    assert result["issues"] is None
    assert result["steps"][0]["data"] == {"ok": True}


async def test_a_capability_can_still_be_bound_after_the_recency_slots_are_full() -> None:
    """The one-way door, named: with every `KEEP_RECENT` slot taken by capabilities already
    used, binding another one used to answer `bound: true` and leave it held back forever."""
    capabilities, context = _crowded()
    await capabilities.probe(context)
    for index in range(KEEP_RECENT):
        capabilities.remember_use(SESSION, f"g{index}")
    held_back = f"g{DEFER_ABOVE}"
    assert held_back not in _bound(capabilities, context)

    await capabilities.execute(_use_plan(held_back), context)

    assert held_back in _bound(capabilities, context)


def test_recency_puts_the_last_used_capability_first() -> None:
    capabilities = Capabilities((HelpPack(),))
    capabilities.remember_use(SESSION, "a")
    capabilities.remember_use(SESSION, "b")
    capabilities.remember_use(SESSION, "a")
    assert capabilities.recent(SESSION) == ("a", "b")


async def test_a_plan_marks_only_the_capabilities_it_actually_used() -> None:
    """Marking every bound capability on every plan made recency mean nothing."""
    capabilities, context = _crowded()
    await capabilities.probe(context)
    await capabilities.execute(_ping_plan("g1"), context)
    assert capabilities.recent(SESSION) == ("g1",)


async def test_asking_for_a_capability_outranks_what_the_same_plan_ran() -> None:
    """Asking is the model saying what it needs next; running is what it just finished."""
    capabilities, context = _crowded()
    await capabilities.probe(context)
    plan = {
        "steps": [
            {"id": "ping", "op": "g1.ping", "input": {}},
            {"id": "bind", "op": "capabilities.use", "input": {"id": f"g{DEFER_ABOVE}"}},
        ]
    }
    await capabilities.execute(plan, context)
    assert capabilities.recent(SESSION) == (f"g{DEFER_ABOVE}", "help", "g1")
    assert context.bound_ids == []


async def test_binding_says_the_capability_is_usable_in_this_same_turn() -> None:
    capabilities, context = _crowded()
    await capabilities.probe(context)
    first = await _use(_run(context, id="g5"))
    again = await _use(_run(context, id="g5"))
    assert first["bound"] is True
    assert "same turn" in first["message"]
    assert "next turn" not in first["message"]
    assert again["bound"] is True
    assert context.bound_ids == ["g5"]


def test_the_prompt_says_a_bind_takes_effect_in_the_next_plan() -> None:
    body = _capabilities(PromptContext(capabilities=("help",), deferred=("workspace",)))
    assert "same turn" in body
    assert "next turn" not in body


async def test_the_capabilities_a_plan_names_are_read_from_the_catalogue() -> None:
    """Not from the operation's prefix: `capabilities.use` belongs to `help`."""
    capabilities, context = _crowded()
    catalogue = await capabilities.probe(context)
    plan = {
        "steps": [
            {"id": "a", "op": "capabilities.use", "input": {}},
            {"id": "b", "op": "g1.ping", "input": {}},
            {"id": "c", "op": "g1.ping", "input": {}},
            {"id": "d", "op": "nowhere.at_all", "input": {}},
            {"id": "e", "op": 42, "input": {}},
            "not a step",
        ]
    }
    assert _packs_run(plan, catalogue) == ("help", "g1")
    assert _packs_run({"steps": "not a list"}, catalogue) == ()
    assert _packs_run({}, catalogue) == ()


# --- end to end, through the supervisor ------------------------------------------------------


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


def _ready_line(request: Request) -> str:
    """The "Ready now:" line of the capabilities section, wherever the request carries it."""
    text = "\n".join([request.system, *(str(message.content) for message in request.messages)])
    return next(line for line in text.splitlines() if line.startswith("Ready now:"))


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def test_one_turn_binds_a_held_back_capability_and_then_uses_it(
    store: SessionStore,
) -> None:
    """The whole path a person hits: ask for something that needs a held-back capability, and
    have it done in that turn -- with the prompt on the second round saying the capability is
    ready, rather than repeating that it is not loaded."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    session = str(created["id"])
    await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "ping it"}], "k"
    )
    held_back = f"g{DEFER_ABOVE}"
    gadgets = tuple(Gadget(f"g{index}") for index in range(DEFER_ABOVE + 1))
    capabilities = Capabilities((HelpPack(), *gadgets))
    provider = ScriptedProvider(
        [
            plans(_use_plan(held_back)),
            plans(_ping_plan(held_back)),
            speaks("Pinged."),
        ]
    )
    running = TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _: provider}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities=capabilities,
    )

    running.wake()
    await running.join()
    await running.aclose()

    first, second, _third = provider.requests
    assert f"{held_back}.ping" not in json.dumps(first.plan_schema)
    assert f"{held_back}.ping" in json.dumps(second.plan_schema)
    assert held_back not in _ready_line(first)
    assert held_back in _ready_line(second)
    turns = await store.records(ACCOUNT, session, "turns")
    assert [turn["status"] for turn in turns] == ["completed"]
    items = await store.records(ACCOUNT, session, "items")
    ran = [
        item["content"]
        for item in items
        if item["type"] == "tool_result" and isinstance(item["content"], dict)
    ]
    assert any(
        step.get("operation") == f"{held_back}.ping" and step.get("status") == "ok" for step in ran
    )
