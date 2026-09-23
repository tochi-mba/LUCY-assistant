"""A setting changed while a turn runs reaches that turn at its next round.

The person asked for it (`apply: "now"`). What that must mean: the next model round is
built with the new mode and the new capability list, is offered a plan schema without the
capability that was turned off, and a plan that names it anyway does not run it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from conftest import ACCOUNT
from weftai.operation import define_operation
from weftai.schema.spec import object_schema
from weftai.schema.types import value

from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.base import Availability, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from weftai.operation import AnyOperation, RunContext


class Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


class Toy:
    """A capability with one operation, which turns the capability off for the session.

    The handler is the person: it does what a `PATCH` with `apply: "now"` does, from inside
    the running turn, so the test needs no second client racing the loop.
    """

    id = "toy"
    title = "Toy"
    summary = "A capability that can switch itself off mid-turn."

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.calls = 0

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[object, ...]:
        return ()

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready)

    def operations(self, context: Any) -> Sequence[AnyOperation]:
        async def switch_off(_run: RunContext[Any]) -> dict[str, Any]:
            self.calls += 1
            await self.store.update(
                context.account_id,
                context.session_id,
                {"disabled_capabilities": ["toy"], "permission_mode": "plan"},
            )
            return {"off": True}

        return (
            define_operation(
                {
                    "name": "toy.off",
                    "description": "Turn this capability off for the session.",
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": switch_off,
                }
            ),
        )


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def test_a_capability_turned_off_mid_turn_is_gone_from_the_next_round(
    store: SessionStore,
) -> None:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    session = str(created["id"])
    await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "toggle"}], "k"
    )
    toy = Toy(store)
    capabilities = Capabilities((HelpPack(), toy))
    off = {"steps": [{"id": "off", "op": "toy.off", "input": {}}]}
    provider = ScriptedProvider([plans(off), plans(off), speaks("It is off now.")])
    events = EventEmitter(SqlEventLog(store), Snapshot())
    running = TurnSupervisor(
        store, ModelRegistry({"scripted": lambda _: provider}), events, capabilities=capabilities
    )

    running.wake()
    await running.join()

    assert toy.calls == 1, "the second plan named an operation that was no longer offered"
    first, second, third = provider.requests
    assert "toy.off" in json.dumps(first.plan_schema)
    assert "toy.off" not in json.dumps(second.plan_schema)
    assert "toy.off" not in json.dumps(third.plan_schema)
    row = await store.get(ACCOUNT, session)
    assert row["permission_mode"] == "plan"
    assert row["disabled_capabilities"] == ["toy"]
    turns = await store.records(ACCOUNT, session, "turns")
    assert [turn["status"] for turn in turns] == ["completed"]
    await running.aclose()
