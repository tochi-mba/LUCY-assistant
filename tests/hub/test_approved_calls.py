"""An approval is an answer about one call: it runs that call, once, and nothing else.

Seen live, in `ask` mode, on the weakest model: a person approved three things -- write
`calc.js`, write `test.js`, run `cd calculator && node test.js`. Node was missing. The turn then
wrote two more files and ran `python test.py`, and the person was asked about none of it,
because a one-time approval was stored under its *permission* and so covered every
`workspace.run` for the rest of the turn. The same keying cut the other way: "no -- call it
`weekly.md` instead" refused the corrected write as well.

And it was the model's job to ask again for what had been approved, "with the same
arguments" -- which a model regenerating a file rarely manages byte for byte. Now the hub runs
the approved call itself when the turn resumes, and the approval is spent.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from conftest import ACCOUNT
from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.clients.testing import Answer, FakeHttp
from lucy_api.context.build import Live
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.base import Availability, Bound, Catalogue, Permission, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.approvals import (
    Ask,
    answer_approval,
    approved_calls,
    approved_plan,
    mark_executed,
    open_approval,
    resumed_notice,
)
from lucy_api.permissions.gate import Grant, PermissionGate, once_key
from lucy_api.permissions.store import grants_for
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.stop import Termination
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path


# --- the gate ------------------------------------------------------------------------------


class Gadget:
    """One write operation under one permission, so the gate has something to ask about."""

    id = "gadget"
    title = "Gadget"
    summary = "A capability with one write."

    def __init__(self, permission: str = "gadget.change") -> None:
        self._permission = permission

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Permission, ...]:
        return (
            Permission(
                id=self._permission,
                title="Change the gadget",
                description="Writes to the gadget.",
                risk="write",
                covers=("gadget.write",),
            ),
        )

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready)

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def run(_run: object) -> dict[str, bool]:
            return {"ok": True}

        return (
            define_operation(
                {
                    "name": "gadget.write",
                    "description": "Write.",
                    "input": object_schema({"path": string_schema()}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": run,
                }
            ),
        )


def _catalogue(permission: str = "gadget.change") -> Catalogue:
    pack = Gadget(permission)
    return Catalogue(
        bound=(
            Bound(
                pack=pack,
                availability=Availability(state=State.ready),
                operations=pack.operations(None),
            ),
        )
    )


def _inspect(arguments: Mapping[str, object], grants: Mapping[str, Grant], **floors: Any) -> Any:
    return PermissionGate().inspect(
        {"steps": [{"id": "w", "op": "gadget.write", "input": dict(arguments)}]},
        mode="ask",
        grants=grants,
        catalogue=_catalogue(floors.pop("permission", "gadget.change")),
        **floors,
    )


def _once(
    arguments: Mapping[str, object], decision: str = "allow", instruction: str = ""
) -> dict[str, Grant]:
    return {
        once_key("gadget.write", arguments): Grant(
            permission="gadget.change",
            decision=decision,
            profile="personal",
            instruction=instruction,
        )
    }


def test_a_one_time_yes_covers_only_the_call_the_person_saw() -> None:
    """The bug, named: a yes to `node test.js` ran `python test.py` too."""
    approved = _once({"path": "node test.js"})
    assert _inspect({"path": "node test.js"}, approved).allowed is True
    other = _inspect({"path": "python test.py"}, approved)
    assert other.allowed is False
    assert other.denied is False, "a different call is asked about, not refused"


def test_a_one_time_no_refuses_only_the_call_it_answered() -> None:
    """ "No -- call it weekly.md instead" must not refuse the corrected write."""
    refused = _once(
        {"path": "review.md"}, decision="deny", instruction="Call it weekly.md instead."
    )
    first = _inspect({"path": "review.md"}, refused)
    assert first.denied is True
    assert "weekly.md" in first.message
    corrected = _inspect({"path": "weekly.md"}, refused)
    assert corrected.denied is False


def test_the_same_call_matches_whatever_order_its_fields_come_in() -> None:
    assert once_key("gadget.write", {"a": 1, "b": 2}) == once_key("gadget.write", {"b": 2, "a": 1})
    assert once_key("gadget.write", {"a": 1}) != once_key("gadget.write", {"a": 2})
    assert once_key("gadget.write", {"a": 1}) != once_key("gadget.read", {"a": 1})


def test_a_standing_grant_still_covers_the_whole_permission() -> None:
    """Session, profile and account answers are about a kind of action, and stay that way."""
    standing = {
        "gadget.change": Grant(permission="gadget.change", decision="allow", profile="personal")
    }
    assert _inspect({"path": "anything"}, standing).allowed is True


def test_a_one_time_yes_cannot_lift_a_floor_a_standing_yes_could_not() -> None:
    """It stands in for the standing grant, through the same floors in the same order."""
    approved = _once({"path": "tea"})
    blocked = _inspect(
        {"path": "tea"}, approved, permission="notes.write", memory_write_policy="never"
    )
    assert blocked.allowed is False
    assert blocked.denied is True


# --- the approvals store --------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def _parked(store: SessionStore, *arguments: dict[str, Any]) -> tuple[str, str, list[str]]:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    session = str(created["id"])
    turn = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k"
    )
    turn_id = str(turn["id"])

    def running(db: sqlite3.Connection) -> None:
        db.execute("UPDATE turns SET status='running' WHERE id=?", (turn_id,))

    await store.worker.call(running)
    ids = [
        await open_approval(
            store,
            account=ACCOUNT,
            session_id=session,
            turn_id=turn_id,
            ask=Ask(
                permission="notes.write",
                operation="notes.remember",
                description="Keep it",
                arguments=args,
            ),
        )
        for args in arguments
    ]
    return session, turn_id, ids


async def _answer(
    store: SessionStore, session: str, approval_id: str, *, approved: bool = True
) -> None:
    await answer_approval(
        store,
        ACCOUNT,
        session,
        {"type": "input.approval", "approval_id": approval_id, "approved": approved},
        f"answer-{approval_id}",
    )


async def test_approved_calls_are_the_granted_ones_not_yet_run_in_the_order_asked(
    store: SessionStore,
) -> None:
    session, turn, ids = await _parked(store, {"title": "a"}, {"title": "b"}, {"title": "c"})
    await _answer(store, session, ids[0])
    await _answer(store, session, ids[1], approved=False)
    await _answer(store, session, ids[2])

    calls = await approved_calls(store, turn)
    assert [call.arguments for call in calls] == [{"title": "a"}, {"title": "c"}]
    assert all(call.operation == "notes.remember" for call in calls)

    await mark_executed(store, calls[:1])
    assert [call.arguments for call in await approved_calls(store, turn)] == [{"title": "c"}]


async def test_a_spent_approval_grants_nothing_on_a_later_claim(store: SessionStore) -> None:
    session, turn, ids = await _parked(store, {"title": "a"})
    await _answer(store, session, ids[0])
    key = once_key("notes.remember", {"title": "a"})
    assert key in await grants_for(store, ACCOUNT, "personal", turn_id=turn)

    await mark_executed(store, await approved_calls(store, turn))
    assert key not in await grants_for(store, ACCOUNT, "personal", turn_id=turn)


async def test_marking_nothing_is_a_no_op_and_marking_twice_keeps_the_first_time(
    store: SessionStore,
) -> None:
    session, turn, ids = await _parked(store, {"title": "a"})
    await _answer(store, session, ids[0])
    await mark_executed(store, ())
    calls = await approved_calls(store, turn)
    await mark_executed(store, calls)

    def when(db: sqlite3.Connection) -> float:
        return float(
            db.execute("SELECT executed_at FROM approvals WHERE id=?", (ids[0],)).fetchone()[0]
        )

    first = await store.worker.call(when)
    await mark_executed(store, calls)
    assert await store.worker.call(when) == first


async def test_an_unreadable_payload_runs_the_call_with_no_arguments(store: SessionStore) -> None:
    session, turn, ids = await _parked(store, {"title": "a"})

    def corrupt(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE approvals SET input_json=? WHERE id=?", ('{"arguments": "oops"}', ids[0])
        )

    await store.worker.call(corrupt)
    await _answer(store, session, ids[0])
    assert (await approved_calls(store, turn))[0].arguments == {}


def test_the_plan_runs_each_call_as_approved_and_nothing_means_no_plan() -> None:
    assert approved_plan(()) is None


def test_the_notice_says_the_calls_already_ran() -> None:
    one = resumed_notice(("notes.remember",))
    two = resumed_notice(("notes.remember", "workspace.write"))
    assert "notes.remember was approved just now and has already run" in one
    assert "its result is above" in one
    assert "were approved just now and have already run" in two
    assert "their results are above" in two
    assert resumed_notice(()) == ""


# --- the loop -------------------------------------------------------------------------------


class _Prompts:
    async def assemble(self, notice: str) -> tuple[str, list[Any]]:
        del notice
        return "You are Lucy.", []


async def test_an_opening_plan_that_parks_ends_the_turn_before_the_model_is_asked() -> None:
    """If the gate changed under the approval -- say the conversation went to plan mode -- the
    approved call is asked about again rather than run, and the model is not called."""
    provider = ScriptedProvider([speaks("never said")])

    async def parks(_plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "issues": [
                {
                    "code": "permission_required",
                    "message": "needs approval",
                    "permission": "notes.write",
                    "operation": "notes.remember",
                    "arguments": {},
                    "description": "Keep it",
                }
            ],
            "text": "needs approval",
            "steps": [],
        }

    outcome = await run_turn(
        Turn(
            provider=provider,
            assemble=_Prompts().assemble,
            execute=parks,
            opening_plan={"steps": [{"id": "approved_1", "op": "notes.remember", "input": {}}]},
        )
    )
    assert outcome.termination is Termination.input_required
    assert provider.remaining == 1


# --- end to end ------------------------------------------------------------------------------

WRITE = {
    "steps": [
        {"id": "remember", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
    ]
}
OTHER = {
    "steps": [
        {"id": "remember", "op": "notes.remember", "input": {"title": "coffee", "body": "no"}}
    ]
}


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


async def _approve_and_resume(
    store: SessionStore, script: list[Any]
) -> tuple[FakeHttp, str, str, ScriptedProvider]:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    conversation = str(created["id"])
    queued = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "Remember tea."}], "q"
    )
    http = FakeHttp(
        *(Answer(body={"data": []}) for _ in range(2)),
        *(Answer(status_code=201, body={"id": f"mem_{n}"}) for n in range(3)),
    )
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
    provider = ScriptedProvider(script)
    running = TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _: provider}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities=capabilities,
    )

    def prepare() -> None:
        running.authorize(
            str(queued["id"]),
            PreparedTurn(pack_context=capabilities.context_for(scope, http=http), live=Live()),
        )

    prepare()
    running.wake()
    await running.join()
    items = await store.records(ACCOUNT, conversation, "items")
    ask = next(item for item in items if item["type"] == "approval_request")
    await answer_approval(
        store,
        ACCOUNT,
        conversation,
        {"type": "input.approval", "approval_id": ask["content"]["approval_id"], "approved": True},
        "approve",
    )
    prepare()
    running.wake()
    await running.join()
    await running.aclose()
    return http, conversation, str(queued["id"]), provider


async def test_a_model_that_plans_the_approved_call_again_is_asked_rather_than_run_twice(
    store: SessionStore,
) -> None:
    """The approval was spent by the run it approved."""
    http, conversation, turn, _provider = await _approve_and_resume(
        store, [plans(WRITE), plans(WRITE), speaks("done")]
    )
    posts = [call for call in http.calls if call.method == "POST"]
    assert len(posts) == 1
    assert (await store.turn(ACCOUNT, turn))["status"] == "input_required"
    asks = [
        i
        for i in await store.records(ACCOUNT, conversation, "items")
        if i["type"] == "approval_request"
    ]
    assert len(asks) == 2


async def test_a_different_call_after_the_approved_one_is_asked_about(store: SessionStore) -> None:
    """The hole, closed: after the approved write ran, the model's next write is its own."""
    http, conversation, turn, _provider = await _approve_and_resume(
        store, [plans(WRITE), plans(OTHER), speaks("done")]
    )
    posts = [call for call in http.calls if call.method == "POST"]
    assert len(posts) == 1
    assert (await store.turn(ACCOUNT, turn))["status"] == "input_required"
    asks = [
        i
        for i in await store.records(ACCOUNT, conversation, "items")
        if i["type"] == "approval_request"
    ]
    assert asks[-1]["content"]["arguments"] == {"title": "coffee", "body": "no"}
