"""An approved call runs with the steps it reads from, exactly as its plan would have.

The bug, named: on 2026-09-30 a plan found a song and played it by reference. The play parked
for approval before anything ran, the person approved it, and the hub ran the approved call on
its own -- with the literal text ``$find_track`` for a track, because the step it read from
was not there. It failed, the model planned the same two steps again, and the turn looped
through ten approvals. Now an approval records its call's step and every step that step reads
from, and the resumed turn runs them together under their own ids.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from conftest import ACCOUNT
from weftai import collection
from weftai.operation import define_operation
from weftai.schema import ref
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.clients.testing import FakeHttp
from lucy_api.context.build import Live
from lucy_api.context.types import Trust
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.base import Availability, Permission, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.approvals import (
    ApprovedCall,
    Ask,
    answer_approval,
    approved_calls,
    open_approval,
)
from lucy_api.permissions.replay import (
    LEGACY_ID,
    Needs,
    digest,
    needs,
    references,
    replay,
)
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path


# --- which steps a call reads from ----------------------------------------------------------

FIND = {"id": "find_track", "op": "music.find", "input": {"name": "Clair de lune"}}
PLAY = {"id": "play", "op": "music.play", "input": {"track": "$find_track"}, "note": "Play it"}
DEVICES = {"id": "devices", "op": "music.devices", "input": {}}
PLAN = {"steps": [DEVICES, FIND, PLAY]}


def test_a_reference_is_a_whole_string_naming_an_earlier_step_wherever_it_sits() -> None:
    value_ = {
        "track": "$find_track",
        "more": ["$hits[1,3]", {"deep": "$hits"}, "$find_track"],
        "text": "costs $5",
        "broken": "$1bad",
        "count": 3,
    }
    assert references(value_) == ("find_track", "hits")
    assert references("$only") == ("only",)
    assert references(None) == ()


def test_a_call_needs_itself_and_every_step_it_reads_from_in_plan_order() -> None:
    chained = {
        "steps": [
            {"id": "search", "op": "research.search", "input": {"q": "x"}},
            DEVICES,
            {"id": "pick", "op": "research.pick", "input": {"from": "$search[1]"}},
            {"id": "keep", "op": "notes.remember", "input": {"about": "$pick"}, "note": "Keep"},
        ]
    }
    found = needs(chained, "keep", parked=("keep",))
    assert [step["id"] for step in found.steps] == ["search", "devices", "pick", "keep"]
    assert found.step == "keep"
    assert found.gated == ()
    assert all(set(step) == {"id", "op", "input"} for step in found.steps)
    assert found.plan == digest(chained)


def test_a_step_it_needs_that_was_parked_beside_it_is_named() -> None:
    plan = {
        "steps": [
            {"id": "fetch", "op": "shelf.fetch", "input": {"name": "x"}},
            {"id": "play", "op": "shelf.play", "input": {"record": "$fetch"}},
        ]
    }
    assert needs(plan, "play", parked=("fetch", "play")).gated == ("fetch",)


def test_an_approved_run_runs_after_the_write_its_plan_put_before_it() -> None:
    """The bug, named: a plan wrote count.py and then ran it; the run parked, was approved, and
    ran alone -- "can't open file count.py" -- and the same two steps parked again, forever."""
    plan = {
        "steps": [
            {"id": "write", "op": "workspace.write", "input": {"path": "count.py"}},
            {"id": "run", "op": "workspace.run", "input": {"command": "python count.py"}},
        ]
    }
    found = needs(plan, "run", parked=("run",))
    assert [step["id"] for step in found.steps] == ["write", "run"]
    assert found.gated == ()


def test_a_step_before_it_that_needed_its_own_yes_is_not_run_for_it() -> None:
    plan = {
        "steps": [
            {"id": "wipe", "op": "workspace.delete", "input": {"path": "old"}},
            {"id": "note", "op": "notes.remember", "input": {"body": "x"}},
            {"id": "run", "op": "workspace.run", "input": {"command": "ls"}},
        ]
    }
    found = needs(plan, "run", parked=("wipe", "run"))
    assert [step["id"] for step in found.steps] == ["note", "run"]
    assert found.gated == (), "an unreferenced refusal does not hold the call back"


def test_a_call_that_reads_from_nothing_needs_only_itself() -> None:
    found = needs(PLAN, "devices")
    assert found.steps == ({"id": "devices", "op": "music.devices", "input": {}},)


def test_a_reference_to_a_step_the_plan_does_not_have_is_left_for_the_executor() -> None:
    plan = {"steps": [{"id": "play", "op": "music.play", "input": {"track": "$gone"}}]}
    assert [step["id"] for step in needs(plan, "play").steps] == ["play"]


def test_a_plan_that_is_not_a_plan_needs_nothing() -> None:
    assert needs(None, "play").steps == ()
    assert needs({"steps": "no"}, "play").steps == ()
    odd = {"steps": ["junk", {"id": "play", "op": "music.play", "input": "not a table"}]}
    assert needs(odd, "play").steps == ({"id": "play", "op": "music.play", "input": {}},)


def test_the_same_plan_has_the_same_digest_whatever_order_its_keys_came_in() -> None:
    reordered = {
        "steps": [
            {**DEVICES},
            {"input": FIND["input"], "op": "music.find", "id": "find_track"},
            PLAY,
        ]
    }
    assert digest(PLAN) == digest(reordered)
    assert digest(PLAN) != digest({"steps": [FIND, PLAY]})
    assert digest(None) == digest({})


# --- the plan a resumed turn runs -----------------------------------------------------------


def call(approval_id: str, recorded: Needs | None, operation: str = "music.play") -> ApprovedCall:
    arguments = dict(PLAY["input"]) if operation == "music.play" else {}
    if recorded is None:
        return ApprovedCall(approval_id, operation, arguments)
    return ApprovedCall(
        approval_id,
        operation,
        arguments,
        step=recorded.step,
        needs=recorded.steps,
        gated=recorded.gated,
        plan=recorded.plan,
    )


def test_an_approved_call_runs_with_the_steps_it_reads_from_under_their_own_ids() -> None:
    played = replay((call("apr_1", needs(PLAN, "play", parked=("play",))),))
    assert played.plan == {
        "steps": [
            {"id": "devices", "op": "music.devices", "input": {}},
            {"id": "find_track", "op": "music.find", "input": {"name": "Clair de lune"}},
            {"id": "play", "op": "music.play", "input": {"track": "$find_track"}},
        ]
    }
    assert [item.approval_id for item in played.ran] == ["apr_1"]
    assert played.held == ()


def test_two_approved_calls_reading_the_same_step_run_it_once() -> None:
    plan = {
        "steps": [
            FIND,
            PLAY,
            {"id": "queue", "op": "music.queue", "input": {"track": "$find_track"}},
        ]
    }
    parked = ("play", "queue")
    played = replay(
        (
            call("apr_1", needs(plan, "play", parked=parked)),
            call("apr_2", needs(plan, "queue", parked=parked), operation="music.queue"),
        )
    )
    assert played.plan is not None
    assert [step["id"] for step in played.plan["steps"]] == ["find_track", "play", "queue"]


def test_a_call_reading_from_a_step_the_person_refused_is_held_and_says_why() -> None:
    plan = {
        "steps": [
            {"id": "fetch", "op": "shelf.fetch", "input": {}},
            {"id": "play", "op": "shelf.play", "input": {"record": "$fetch"}},
        ]
    }
    parked = ("fetch", "play")
    only_play = call("apr_2", needs(plan, "play", parked=parked), operation="shelf.play")
    played = replay((only_play,))
    assert played.plan is None
    assert played.ran == ()
    [(held, why)] = played.held
    assert held is only_play
    assert why == (
        "shelf.play was approved but did not run: it reads from fetch, "
        "which the person did not approve."
    )


def test_both_approved_the_parked_dependency_runs_first() -> None:
    plan = {
        "steps": [
            {"id": "fetch", "op": "shelf.fetch", "input": {}},
            {"id": "play", "op": "shelf.play", "input": {"record": "$fetch"}},
        ]
    }
    parked = ("fetch", "play")
    played = replay(
        (
            call("apr_1", needs(plan, "fetch", parked=parked), operation="shelf.fetch"),
            call("apr_2", needs(plan, "play", parked=parked), operation="shelf.play"),
        )
    )
    assert played.plan is not None
    assert [step["id"] for step in played.plan["steps"]] == ["fetch", "play"]
    assert played.held == ()


def test_a_call_from_another_plan_whose_step_ids_clash_is_held() -> None:
    other = {"steps": [{"id": "find_track", "op": "music.find", "input": {"name": "Else"}}, PLAY]}
    played = replay(
        (
            call("apr_1", needs(PLAN, "play", parked=("play",))),
            call("apr_2", needs(other, "play", parked=("play",))),
        )
    )
    assert [item.approval_id for item in played.ran] == ["apr_1"]
    [(_held, why)] = played.held
    assert why == (
        "music.play was approved but did not run: it came from a different plan, whose step "
        "find_track is not the one that ran."
    )


def test_an_approval_recorded_without_its_step_runs_on_its_own_as_before() -> None:
    played = replay((call("apr_1", None), call("apr_2", None, operation="music.pause")))
    assert played.plan == {
        "steps": [
            {"id": LEGACY_ID.format(index=1), "op": "music.play", "input": PLAY["input"]},
            {"id": LEGACY_ID.format(index=2), "op": "music.pause", "input": {}},
        ]
    }


def test_an_approval_recorded_without_its_step_is_held_when_its_only_id_is_taken() -> None:
    """The bug, named: the step a stepped call read from was replaced, without a word, by
    an older approval whose only possible id happened to be the same."""
    find_a = {"id": "approved_2", "op": "music.find", "input": {"name": "a"}}
    play_it = {"id": "play", "op": "music.play", "input": {"track": "$approved_2"}}
    stepped = ApprovedCall(
        approval_id="apr_1",
        operation="music.play",
        arguments=play_it["input"],
        step="play",
        needs=(find_a, play_it),
        plan="p",
    )
    legacy = ApprovedCall(approval_id="apr_2", operation="music.fetch", arguments={"name": "z"})
    result = replay([stepped, legacy])
    assert result.plan == {"steps": [find_a, play_it]}
    assert result.ran == (stepped,)
    assert result.held == (
        (
            legacy,
            "music.fetch was approved but did not run: another approved step already runs "
            "as approved_2, the only id it could have.",
        ),
    )


def test_nothing_approved_is_nothing_to_run() -> None:
    assert replay(()).plan is None


# --- the approval record keeps what its call needs --------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def _approved(
    store: SessionStore, recorded: Needs | None, key: str = "key"
) -> tuple[str, list[ApprovedCall]]:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), key)
    session = str(created["id"])
    turn = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], f"{key}-turn"
    )
    turn_id = str(turn["id"])

    def running(db: sqlite3.Connection) -> None:
        db.execute("UPDATE turns SET status='running' WHERE id=?", (turn_id,))

    await store.worker.call(running)
    approval = await open_approval(
        store,
        account=ACCOUNT,
        session_id=session,
        turn_id=turn_id,
        ask=Ask(
            permission="music.control",
            operation="music.play",
            description="Play it",
            arguments=dict(PLAY["input"]),
            needs=recorded,
        ),
    )
    await answer_approval(
        store,
        ACCOUNT,
        session,
        {"type": "input.approval", "approval_id": approval, "approved": True},
        f"{key}-answer",
    )
    return approval, list(await approved_calls(store, turn_id))


async def test_an_approval_keeps_its_step_and_what_it_reads_from(store: SessionStore) -> None:
    recorded = needs(PLAN, "play", parked=("play",))
    _approval, [back] = await _approved(store, recorded)
    assert (back.step, back.needs, back.gated, back.plan) == (
        "play",
        recorded.steps,
        (),
        recorded.plan,
    )


async def test_an_ask_without_a_step_records_nothing_to_replay(store: SessionStore) -> None:
    _approval, [back] = await _approved(store, None)
    assert (back.step, back.needs, back.gated, back.plan) == ("", (), (), "")
    _approval, [blank] = await _approved(
        store, Needs(step="", steps=(), gated=(), plan="p"), key="blank"
    )
    assert blank.step == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"step": 7},
        {"needs": "not a list"},
        {"needs": [{"id": "play", "op": "music.play", "input": "not a table"}]},
        {"gated": [1]},
        {"plan": None},
    ],
)
async def test_replay_fields_that_cannot_be_read_run_the_call_on_its_own(
    store: SessionStore, fields: dict[str, Any]
) -> None:
    approval, _ = await _approved(store, needs(PLAN, "play", parked=("play",)))

    def spoil(db: sqlite3.Connection) -> None:
        import json

        row = db.execute("SELECT input_json FROM approvals WHERE id=?", (approval,)).fetchone()
        payload = {**json.loads(row[0]), **fields}
        db.execute(
            "UPDATE approvals SET input_json=?, executed_at=NULL WHERE id=?",
            (json.dumps(payload), approval),
        )

    await store.worker.call(spoil)
    turn_row = await store.worker.call(
        lambda db: db.execute("SELECT turn_id FROM approvals WHERE id=?", (approval,)).fetchone()
    )
    [back] = await approved_calls(store, str(turn_row[0]))
    assert (back.step, back.needs) == ("", ())
    assert back.arguments == PLAY["input"]


# --- end to end: find, approve the play, and it plays what was found --------------------------

RECORD = collection(
    "record",
    dict,
    label=lambda item: str(item.get("name") or ""),
    key=lambda item: str(item.get("uri") or ""),
    description="Something on the shelf that can be played.",
)


class Shelf:
    """A read that finds records, a gated fetch that returns them, and a gated play that takes
    a reference to them -- the shape of music's find and play, with nothing else attached."""

    id = "shelf"
    title = "Shelf"
    summary = "Find and play records."

    def __init__(self) -> None:
        self.played: list[list[str]] = []

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Permission, ...]:
        return (
            Permission(
                id="shelf.change",
                title="Play from the shelf",
                description="Plays records.",
                risk="write",
                covers=("shelf.play",),
            ),
            # Its own permission, so a person can refuse the fetch and approve the play: one
            # plan's calls under one permission are one card, answered once.
            Permission(
                id="shelf.stock",
                title="Fetch from storage",
                description="Fetches records.",
                risk="write",
                covers=("shelf.fetch",),
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
        async def find(run: Any) -> list[dict[str, str]]:
            name = str(run.input.get("name") or "")
            return [{"uri": f"shelf:{name}", "name": name}]

        async def play(run: Any) -> dict[str, Any]:
            uris = [str(item["uri"]) for item in run.input["record"].items]
            self.played.append(uris)
            return {"playing": uris}

        name = {"name": string_schema()}
        return (
            define_operation(
                {
                    "name": "shelf.find",
                    "description": "Find a record.",
                    "input": object_schema(name),
                    "output": RECORD,
                    "effects": "read",
                    "run": find,
                }
            ),
            define_operation(
                {
                    "name": "shelf.fetch",
                    "description": "Fetch a record from storage.",
                    "input": object_schema(name),
                    "output": RECORD,
                    "effects": "write",
                    "run": find,
                }
            ),
            define_operation(
                {
                    "name": "shelf.play",
                    "description": "Play records.",
                    "input": object_schema({"record": ref(RECORD)}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": play,
                }
            ),
        )


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


async def _hold(
    store: SessionStore, script: list[Any], answers: Mapping[str, bool]
) -> tuple[Shelf, list[dict[str, Any]], str, ScriptedProvider]:
    """One message, parked; each ask answered by operation; resumed; everything it wrote."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    conversation = str(created["id"])
    queued = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "Play x."}], "q"
    )
    shelf = Shelf()
    capabilities = Capabilities((HelpPack(), shelf))
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
            PreparedTurn(
                pack_context=capabilities.context_for(scope, http=FakeHttp()), live=Live()
            ),
        )

    prepare()
    running.wake()
    await running.join()
    items = await store.records(ACCOUNT, conversation, "items")
    for item in items:
        if item["type"] != "approval_request":
            continue
        content = item["content"]
        await answer_approval(
            store,
            ACCOUNT,
            conversation,
            {
                "type": "input.approval",
                "approval_id": content["approval_id"],
                "approved": answers[content["tool"]],
            },
            f"answer-{content['approval_id']}",
        )
    prepare()
    running.wake()
    await running.join()
    await running.aclose()
    items = await store.records(ACCOUNT, conversation, "items")
    return shelf, items, str(queued["id"]), provider


FIND_AND_PLAY = {
    "steps": [
        {"id": "found", "op": "shelf.find", "input": {"name": "x"}, "note": "Find x"},
        {"id": "play", "op": "shelf.play", "input": {"record": "$found"}, "note": "Play x"},
    ]
}


async def test_an_approved_play_plays_the_record_its_plan_found(store: SessionStore) -> None:
    """The bug, named: the approved call ran alone and could not resolve ``$found``."""
    shelf, items, turn, provider = await _hold(
        store, [plans(FIND_AND_PLAY), speaks("Playing x.")], {"shelf.play": True}
    )
    assert shelf.played == [["shelf:x"]]
    ran = [item["content"] for item in items if item["type"] == "tool_result"]
    assert [(step["operation"], step["status"]) for step in ran] == [
        ("shelf.find", "ok"),
        ("shelf.play", "ok"),
    ]
    assert [step["step_id"] for step in ran] == ["found", "play"]
    [ask] = [item["content"] for item in items if item["type"] == "approval_request"]
    assert ask["arguments"] == {"record": "$found"}
    assert (await store.turn(ACCOUNT, turn))["status"] == "completed"
    assert provider.remaining == 0


async def test_the_approval_card_says_what_the_step_s_note_says(store: SessionStore) -> None:
    """The bug, named: the turn loop took Lucy's own fields off a plan before the gate saw it,
    so no approval card carried the step's note, only the gate's generic sentence."""
    _shelf, items, _turn, _provider = await _hold(
        store, [plans(FIND_AND_PLAY), speaks("Playing x.")], {"shelf.play": True}
    )
    [ask] = [item["content"] for item in items if item["type"] == "approval_request"]
    assert ask["description"].startswith("Play x")


async def test_a_card_names_what_a_referenced_step_looks_for(store: SessionStore) -> None:
    """The bug, named: no step in a parked plan runs before the card is answered, so a play
    read `{"track": "$found"}` and the card said "Start playing it." -- the person approved a
    song nobody had named."""
    _shelf, items, _turn, _provider = await _hold(
        store, [plans(FIND_AND_PLAY), speaks("Playing x.")], {"shelf.play": True}
    )
    [ask] = [item["content"] for item in items if item["type"] == "approval_request"]
    assert ask["description"] == "Play x (record: what shelf.find returns for name 'x')"


def test_a_referenced_step_is_described_by_its_plain_inputs_only() -> None:
    from lucy_api.permissions.approvals import _said, _what_it_reads
    from lucy_api.permissions.replay import needs

    plan = {
        "steps": [
            {"id": "a", "op": "shelf.find", "input": {"name": "y" * 80, "year": 1999, "ok": True}},
            {"id": "b", "op": "shelf.pick", "input": {"from": "$a"}},
            {"id": "c", "op": "shelf.play", "input": {"record": "$b", "also": "$gone"}},
        ]
    }
    said = _what_it_reads(needs(plan, "c"), {"record": "$b", "also": "$gone"})
    assert said == "record: what shelf.pick returns for what it was given"
    assert _said({"name": "y" * 80, "year": 1999, "ok": True}) == (
        f"name '{'y' * 59}\N{HORIZONTAL ELLIPSIS}', year 1999"
    )
    assert _said("not a mapping") == "what it was given"
    assert _what_it_reads(None, {"record": "$b"}) == ""


async def test_a_play_that_reads_from_a_refused_fetch_does_not_run(store: SessionStore) -> None:
    fetch_and_play = {
        "steps": [
            {"id": "got", "op": "shelf.fetch", "input": {"name": "x"}, "note": "Fetch x"},
            {"id": "play", "op": "shelf.play", "input": {"record": "$got"}, "note": "Play x"},
        ]
    }
    shelf, items, _turn, provider = await _hold(
        store,
        [plans(fetch_and_play), speaks("I could not play it: fetching was refused.")],
        {"shelf.fetch": False, "shelf.play": True},
    )
    assert shelf.played == []
    assert [item for item in items if item["type"] == "tool_result"] == []
    assert provider.remaining == 0
    notices = " ".join(str(request) for request in provider.requests)
    assert (
        "shelf.play was approved but did not run: it reads from got, "
        "which the person did not approve."
    ) in notices
