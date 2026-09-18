"""Parking a write on a person, and resuming it from their answer.

The client's `approved` flag is an input. The grant is recorded here, and the next claim
re-runs the gate, which is why a forged approval cannot widen what the tool may do.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.packs.base import Availability, Bound, Catalogue, State
from lucy_api.packs.notes import NotesPack
from lucy_api.permissions.approvals import Ask, answer_approval, open_approval
from lucy_api.permissions.gate import ACCOUNT_PROFILE, PermissionGate
from lucy_api.permissions.store import (
    SESSION_PROFILE_PREFIX,
    delete_grant,
    grants_for,
    list_grants,
    put_grant,
)
from lucy_api.sessions.models import CreateSession, Outcome
from lucy_api.sessions.sql_store import identifier
from lucy_api.sessions.turns import close_turn, open_turn

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore

OWNER = "acct_owner"
STRANGER = "acct_stranger"
SPOKE = {"events": [{"type": "input.message", "content": "remember this"}]}


async def a_session(store: SessionStore, account: str = OWNER, **fields: object) -> str:
    created = await store.create(account, CreateSession(**fields), identifier("key"))
    return str(created["id"])


async def a_running_turn(store: SessionStore, session: str) -> str:
    turn = await open_turn(store, OWNER, session, SPOKE)
    await close_turn(store, OWNER, str(turn["id"]), Outcome("running"))
    return str(turn["id"])


async def park(store: SessionStore, session: str, turn: str) -> str:
    return await open_approval(
        store,
        account=OWNER,
        session_id=session,
        turn_id=turn,
        ask=Ask(
            permission="notes.write",
            operation="notes.remember",
            description="Keep the tea preference",
            arguments={"title": "tea", "body": "green"},
            policy="ask",
        ),
    )


async def test_parking_a_write_leaves_an_approval_the_person_can_answer(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)

    approval_id = await park(sessions_store, session, turn)

    parked = await sessions_store.turn(OWNER, turn)
    items = await sessions_store.records(OWNER, session, "items")
    events = [event["type"] for event in await sessions_store.records(OWNER, session, "events")]
    assert parked["status"] == "input_required"
    assert items[-1]["type"] == "approval_request"
    assert items[-1]["content"]["approval_id"] == approval_id
    assert items[-1]["content"]["tool"] == "notes.remember"
    assert "lucy.approval.requested" in events
    assert (await sessions_store.get(OWNER, session))["status"] == "input_required"


async def test_approving_once_requeues_the_turn_and_is_visible_only_on_that_claim(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    decided = await answer_approval(
        sessions_store,
        OWNER,
        session,
        {"type": "input.approval", "approval_id": approval_id, "approved": True},
        "once-key",
    )

    grants = await grants_for(sessions_store, OWNER, "personal", session_id=session, turn_id=turn)
    later = await grants_for(sessions_store, OWNER, "personal", session_id=session)
    assert decided.turn["id"] == turn
    assert decided.turn["status"] == "queued"
    assert grants["notes.write"].decision == "allow"
    assert "notes.write" not in later


async def test_an_account_lifetime_grant_survives_into_the_next_session(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {
            "type": "input.approval",
            "approval_id": approval_id,
            "approved": True,
            "lifetime": "account",
        },
        "account-key",
    )

    other = await a_session(sessions_store)
    grants = await grants_for(sessions_store, OWNER, "work", session_id=other)
    assert grants["notes.write"].profile == ACCOUNT_PROFILE
    assert grants["notes.write"].decision == "allow"


async def test_a_session_lifetime_grant_does_not_follow_the_person_elsewhere(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {
            "type": "input.approval",
            "approval_id": approval_id,
            "approved": True,
            "lifetime": "session",
        },
        "session-key",
    )

    here = await grants_for(sessions_store, OWNER, "personal", session_id=session)
    elsewhere = await grants_for(
        sessions_store, OWNER, "personal", session_id=await a_session(sessions_store)
    )
    assert here["notes.write"].profile == SESSION_PROFILE_PREFIX + session
    assert "notes.write" not in elsewhere


async def test_answering_one_of_two_asks_leaves_the_unresolved_write_parked(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    first = await park(sessions_store, session, turn)
    second = await open_approval(
        sessions_store,
        account=OWNER,
        session_id=session,
        turn_id=turn,
        ask=Ask(
            permission="notes.write",
            operation="notes.forget",
            description="Drop the coffee note",
            arguments={"id": "mem_1"},
        ),
    )

    decided = await answer_approval(
        sessions_store,
        OWNER,
        session,
        {"type": "input.approval", "approval_id": first, "approved": True},
        "subset-key",
    )

    assert decided.turn["status"] == "input_required"
    leftover = await sessions_store.turn(OWNER, turn)
    assert leftover["status"] == "input_required"

    finished = await answer_approval(
        sessions_store,
        OWNER,
        session,
        {"type": "input.approval", "approval_id": second, "approved": False},
        "subset-done",
    )
    assert finished.turn["status"] == "queued"


async def test_a_grant_and_a_refusal_are_both_audit_rows(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)
    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {
            "type": "input.approval",
            "approval_id": approval_id,
            "approved": True,
            "lifetime": "profile",
        },
        "audit-key",
    )
    await put_grant(
        sessions_store,
        OWNER,
        permission="workspace.files",
        profile="personal",
        decision="deny",
        instruction="Stay out of archive/",
    )

    rows = await sessions_store.audit_log(OWNER)
    actions = [row["action"] for row in rows]
    assert "permission.requested" in actions
    assert "permission.granted" in actions
    assert "permission.denied" in actions
    assert all("green" not in str(row["detail"]) for row in rows)


async def test_denying_requeues_the_turn_with_an_instruction_the_gate_will_see(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {
            "type": "input.approval",
            "approval_id": approval_id,
            "approved": False,
            "instruction": "Ask me first next time.",
        },
        "deny-key",
    )

    grants = await grants_for(sessions_store, OWNER, "personal", session_id=session, turn_id=turn)
    items = await sessions_store.records(OWNER, session, "items")
    assert grants["notes.write"].decision == "deny"
    assert grants["notes.write"].instruction == "Ask me first next time."
    assert items[-1]["type"] == "approval_response"
    assert items[-1]["content"]["approved"] is False


async def test_another_account_cannot_see_or_answer_the_ask(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    with pytest.raises(LucyError) as caught:
        await answer_approval(
            sessions_store,
            STRANGER,
            session,
            {"type": "input.approval", "approval_id": approval_id, "approved": True},
            "stolen-key",
        )

    assert caught.value.code == "not-found"
    assert (await sessions_store.turn(OWNER, turn))["status"] == "input_required"


async def test_a_decided_approval_is_the_same_miss_as_an_unknown_one(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)
    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {"type": "input.approval", "approval_id": approval_id, "approved": True},
        "first-key",
    )

    with pytest.raises(LucyError) as caught:
        await answer_approval(
            sessions_store,
            OWNER,
            session,
            {"type": "input.approval", "approval_id": approval_id, "approved": True},
            "second-key",
        )

    assert caught.value.code == "not-found"


async def test_replaying_the_same_approval_key_does_not_queue_the_turn_twice(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)
    event = {"type": "input.approval", "approval_id": approval_id, "approved": True}

    first = await answer_approval(sessions_store, OWNER, session, event, "same-key")
    again = await answer_approval(sessions_store, OWNER, session, event, "same-key")

    assert first.turn["id"] == again.turn["id"] == turn
    items = [
        item
        for item in await sessions_store.records(OWNER, session, "items")
        if item["type"] == "approval_response"
    ]
    assert len(items) == 1


async def test_a_session_grant_overrides_an_account_grant_for_the_same_permission(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await put_grant(
        sessions_store,
        OWNER,
        permission="notes.write",
        profile=ACCOUNT_PROFILE,
        decision="allow",
    )
    await put_grant(
        sessions_store,
        OWNER,
        permission="notes.write",
        profile=SESSION_PROFILE_PREFIX + session,
        decision="deny",
        instruction="Not in this conversation.",
    )

    grants = await grants_for(sessions_store, OWNER, "personal", session_id=session)

    assert grants["notes.write"].decision == "deny"
    assert grants["notes.write"].instruction == "Not in this conversation."


async def test_a_profile_lifetime_stays_on_that_profile(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {
            "type": "input.approval",
            "approval_id": approval_id,
            "approved": True,
            "lifetime": "profile",
        },
        "profile-key",
    )

    grants = await grants_for(sessions_store, OWNER, "personal")
    other = await grants_for(sessions_store, OWNER, "work")
    assert grants["notes.write"].profile == "personal"
    assert "notes.write" not in other


async def test_parking_a_turn_that_is_not_running_is_a_conflict(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)

    with pytest.raises(LucyError) as caught:
        await park(sessions_store, session, str(turn["id"]))

    assert caught.value.code == "conflict"


async def test_an_approval_without_an_id_or_a_boolean_is_a_conflict(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    with pytest.raises(LucyError) as missing:
        await answer_approval(
            sessions_store, OWNER, session, {"type": "input.approval", "approved": True}, "no-id"
        )
    with pytest.raises(LucyError) as flag:
        await answer_approval(
            sessions_store,
            OWNER,
            session,
            {"type": "input.approval", "approval_id": "apr_x", "approved": "yes"},
            "bad-flag",
        )

    assert missing.value.code == flag.value.code == "conflict"


async def test_answering_after_the_turn_has_moved_on_is_a_conflict(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)
    await close_turn(sessions_store, OWNER, turn, Outcome("completed", "success", "end_turn"))

    with pytest.raises(LucyError) as caught:
        await answer_approval(
            sessions_store,
            OWNER,
            session,
            {"type": "input.approval", "approval_id": approval_id, "approved": True},
            "too-late",
        )

    assert caught.value.code == "conflict"


async def test_parking_a_turn_that_already_finished_is_a_conflict(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    await close_turn(sessions_store, OWNER, turn, Outcome("completed", "success", "end_turn"))

    with pytest.raises(LucyError) as caught:
        await park(sessions_store, session, turn)

    assert caught.value.code == "conflict"
    assert "no longer running" in str(caught.value)


async def test_parking_while_another_turn_is_queued_keeps_the_session_queued(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    running = await a_running_turn(sessions_store, session)
    await open_turn(sessions_store, OWNER, session, SPOKE)

    await park(sessions_store, session, running)

    assert (await sessions_store.get(OWNER, session))["status"] == "queued"


async def test_an_ask_without_an_operation_is_labelled_by_its_permission(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)

    await open_approval(
        sessions_store,
        account=OWNER,
        session_id=session,
        turn_id=turn,
        ask=Ask(permission="notes.write", operation="", description=""),
    )

    item = (await sessions_store.records(OWNER, session, "items"))[-1]
    assert item["content"]["tool"] == "notes.write"
    assert item["content"]["description"] == "notes.write"


async def test_a_oneshot_with_a_malformed_payload_still_uses_the_operation(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    def corrupt(db: sqlite3.Connection) -> None:
        db.execute("UPDATE approvals SET input_json=? WHERE id=?", ("[]", approval_id))

    await sessions_store.worker.call(corrupt)
    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {"type": "input.approval", "approval_id": approval_id, "approved": True},
        "malformed-payload",
    )

    grants = await grants_for(sessions_store, OWNER, "personal", turn_id=turn)
    assert grants["notes.remember"].decision == "allow"


async def test_a_oneshot_with_no_payload_still_uses_the_operation(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)
    approval_id = await park(sessions_store, session, turn)

    def clear(db: sqlite3.Connection) -> None:
        db.execute("UPDATE approvals SET input_json=? WHERE id=?", ("", approval_id))

    await sessions_store.worker.call(clear)
    await answer_approval(
        sessions_store,
        OWNER,
        session,
        {"type": "input.approval", "approval_id": approval_id, "approved": False},
        "empty-payload",
    )

    grants = await grants_for(sessions_store, OWNER, "personal", turn_id=turn)
    assert grants["notes.remember"].decision == "deny"


async def test_an_account_wide_profile_name_is_not_listed_twice(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store,
        OWNER,
        permission="notes.write",
        profile=ACCOUNT_PROFILE,
        decision="allow",
    )

    grants = await grants_for(sessions_store, OWNER, ACCOUNT_PROFILE, session_id="ses_x")

    assert grants["notes.write"].profile == ACCOUNT_PROFILE


async def test_a_grant_on_another_profile_does_not_leak_into_this_one(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store,
        OWNER,
        permission="notes.write",
        profile="work",
        decision="deny",
    )

    grants = await grants_for(sessions_store, OWNER, "personal")

    assert "notes.write" not in grants


async def test_listing_and_deleting_grants_is_scoped_to_the_account(
    sessions_store: SessionStore,
) -> None:
    await put_grant(
        sessions_store, OWNER, permission="notes.write", profile="personal", decision="allow"
    )
    await put_grant(
        sessions_store, STRANGER, permission="notes.write", profile="personal", decision="deny"
    )

    listed = await list_grants(sessions_store, OWNER)
    await delete_grant(sessions_store, OWNER, "notes.write", profile="personal")

    assert [row["permission"] for row in listed] == ["notes.write"]
    assert await grants_for(sessions_store, OWNER, "personal") == {}
    with pytest.raises(LucyError) as caught:
        await delete_grant(sessions_store, OWNER, "notes.write", profile="personal")
    assert caught.value.code == "not-found"


def test_a_missing_catalogue_or_a_non_list_of_steps_asks_about_nothing() -> None:
    gate = PermissionGate()
    empty = gate.inspect(
        {"steps": [{"op": "notes.remember"}]}, mode="ask", grants={}, catalogue=None
    )
    broken = gate.inspect(
        {"steps": {"op": "notes.remember"}}, mode="ask", grants={}, catalogue=None
    )
    assert empty.allowed is True
    assert broken.allowed is True


def test_a_write_permission_is_not_asked_about_when_its_operations_are_not_bound() -> None:
    catalogue = Catalogue(
        bound=(
            Bound(
                pack=NotesPack("http://memory.test"),
                availability=Availability(state=State.ready),
                operations=(),
            ),
        )
    )
    verdict = PermissionGate().inspect(
        {"steps": [{"op": "notes.remember"}]},
        mode="ask",
        grants={},
        catalogue=catalogue,
    )
    assert verdict.allowed is True


def test_an_unknown_operation_is_treated_as_a_read() -> None:
    from lucy_api.permissions.gate import _effects

    assert _effects("notes.remember", None) == "read"


def test_a_non_object_approval_payload_contributes_no_fields() -> None:
    from lucy_api.permissions import approvals as module

    assert module._payload(None) == {}
    assert module._payload("") == {}
    assert module._payload("[]") == {}
    assert module._payload('{"permission":"notes.write"}') == {"permission": "notes.write"}
