"""An approved call that cannot run is held and its grant goes with it, and one whose replay
parks again is kept.

The bugs, named, each found by reviewing the replay of approved calls on 2026-09-30:

- A call held because it read from a refused step kept its one-time grant, so the model's
  next plan ran the same call with another source and nobody was asked.
- A step a standing deny refused was not among the steps a call was recorded as needing, so
  the call was not held: the whole resumed plan was refused, every approved call in it was
  lost, and the model was told they had run.
- A resumed plan that parked again, because the mode changed after the person answered,
  consumed the approvals it carried; those calls never ran and nobody was told.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from conftest import ACCOUNT
from vault_pack import Vault

from lucy_api.clients.testing import FakeHttp
from lucy_api.context.build import Live
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.approvals import answer_approval
from lucy_api.permissions.store import put_grant
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.turns import submit_messages
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucy_api.sessions.sql_store import SessionStore


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


Cycle = tuple[str, dict[str, bool]]
"""The permission mode a claim runs under, and the answer to each ask pending before it."""


async def _converse(
    store: SessionStore, script: list[Any], cycles: Sequence[Cycle]
) -> tuple[Vault, list[dict[str, Any]], str, ScriptedProvider]:
    """One message, claimed once per cycle; each pending ask answered by operation first."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    conversation = str(created["id"])
    queued = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "Play x."}], "q"
    )
    vault = Vault()
    capabilities = Capabilities((HelpPack(), vault))
    provider = ScriptedProvider(script)
    running = TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _: provider}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities=capabilities,
    )
    answered: set[str] = set()
    for mode, answers in cycles:
        for item in await store.records(ACCOUNT, conversation, "items"):
            content = item["content"]
            if item["type"] != "approval_request" or content["approval_id"] in answered:
                continue
            if content["tool"] not in answers:
                continue
            answered.add(content["approval_id"])
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
        # The supervisor reads the mode from the session, as a person's change reaches it.
        await store.update(ACCOUNT, conversation, {"permission_mode": mode})
        scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
        running.authorize(
            str(queued["id"]),
            PreparedTurn(
                pack_context=capabilities.context_for(scope, http=FakeHttp()), live=Live()
            ),
        )
        running.wake()
        await running.join()
    await running.aclose()
    items = await store.records(ACCOUNT, conversation, "items")
    return vault, items, str(queued["id"]), provider


def _asks(items: list[dict[str, Any]]) -> list[str]:
    return [item["content"]["tool"] for item in items if item["type"] == "approval_request"]


def _ran(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (item["content"]["operation"], item["content"]["status"])
        for item in items
        if item["type"] == "tool_result"
    ]


def _notices(provider: ScriptedProvider) -> str:
    return " ".join(str(request) for request in provider.requests)


FETCH_AND_PLAY = {
    "steps": [
        {"id": "got", "op": "vault.fetch", "input": {"name": "x"}, "note": "Fetch x"},
        {"id": "play", "op": "vault.play", "input": {"record": "$got"}, "note": "Play x"},
    ]
}


async def test_a_held_call_s_grant_does_not_let_the_next_plan_run_it_with_another_source(
    sessions_store: SessionStore,
) -> None:
    """The bug, named: play was held because fetch was refused, its one-time grant stayed
    live, and the model's next plan played whatever it chose to find instead, unasked."""
    replan = {
        "steps": [
            {"id": "got", "op": "vault.find", "input": {"name": "evil"}, "note": "Find evil"},
            {"id": "play", "op": "vault.play", "input": {"record": "$got"}, "note": "Play evil"},
        ]
    }
    vault, items, _turn, provider = await _converse(
        sessions_store,
        [plans(FETCH_AND_PLAY), plans(replan), speaks("Played it.")],
        [("ask", {}), ("ask", {"vault.fetch": False, "vault.play": True})],
    )
    assert vault.played == []
    assert _asks(items) == ["vault.fetch", "vault.play", "vault.play"]
    assert provider.remaining == 1


async def test_a_call_reading_from_a_step_a_standing_deny_refused_is_held_and_the_rest_runs(
    sessions_store: SessionStore,
) -> None:
    """The bug, named: the refused fetch was not recorded as something play needed, play was
    not held, the whole resumed plan was refused, mark was lost with it, and the model was
    told both had run."""
    await put_grant(
        sessions_store, ACCOUNT, permission="vault.take", profile="personal", decision="deny"
    )
    plan = {
        "steps": [
            *FETCH_AND_PLAY["steps"],
            {"id": "tag", "op": "vault.mark", "input": {"name": "y"}, "note": "Mark y"},
        ]
    }
    vault, items, turn, provider = await _converse(
        sessions_store,
        [plans(plan), speaks("Marked y; I could not play x.")],
        [("ask", {}), ("ask", {"vault.play": True, "vault.mark": True})],
    )
    assert _asks(items) == ["vault.play", "vault.mark"]
    assert vault.marked == ["y"]
    assert vault.played == []
    assert vault.fetched == []
    assert _ran(items) == [("vault.mark", "ok")]
    notices = _notices(provider)
    assert "vault.mark was approved just now and has already run" in notices
    assert (
        "vault.play was approved but did not run: it reads from got, "
        "which the person did not approve."
    ) in notices
    assert (await sessions_store.turn(ACCOUNT, turn))["status"] == "completed"


async def test_an_approved_call_whose_replay_parks_again_runs_once_the_new_ask_is_answered(
    sessions_store: SessionStore,
) -> None:
    """The bug, named: under auto the fetch ran unasked and the play was approved; the
    session was switched to ask before the answer was picked up, the replay parked on the
    fetch, and the play's approval was consumed with the play never run."""
    vault, items, turn, provider = await _converse(
        sessions_store,
        [plans(FETCH_AND_PLAY), speaks("Played x.")],
        [("auto", {}), ("ask", {"vault.play": True}), ("ask", {"vault.fetch": True})],
    )
    assert _asks(items) == ["vault.play", "vault.fetch"]
    assert vault.fetched == ["x"]
    assert vault.played == [["vault:x"]]
    assert _ran(items) == [("vault.fetch", "ok"), ("vault.play", "ok")]
    assert (await sessions_store.turn(ACCOUNT, turn))["status"] == "completed"
    assert provider.remaining == 0


async def test_a_replay_that_parks_again_and_is_then_refused_holds_the_call(
    sessions_store: SessionStore,
) -> None:
    """Given back against the plan that parked, the call now needs the step that parked
    beside it; refusing that step holds the call rather than refusing the plan."""
    vault, items, _turn, provider = await _converse(
        sessions_store,
        [plans(FETCH_AND_PLAY), speaks("I could not play x.")],
        [("auto", {}), ("ask", {"vault.play": True}), ("ask", {"vault.fetch": False})],
    )
    assert _asks(items) == ["vault.play", "vault.fetch"]
    assert vault.played == []
    assert _ran(items) == []
    assert (
        "vault.play was approved but did not run: it reads from got, "
        "which the person did not approve."
    ) in _notices(provider)
