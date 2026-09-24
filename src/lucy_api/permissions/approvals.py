"""Parking a turn on a person, and resuming it from their answer.

The gate decides that a write may not run yet. This module is the other half: a durable
approval row, an item a client can show, and the one input event that unblocks the turn.
The client's `approved: true` is an input, not an authorization — the grant is recorded
here, and the next claim of the turn re-runs the gate against it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, cast

from lucy_api.core.errors import absent, conflict
from lucy_api.permissions.gate import ACCOUNT_PROFILE, Grant
from lucy_api.permissions.store import SESSION_PROFILE_PREFIX
from lucy_api.sessions.sql_store import (
    IdempotentWrite,
    NewItem,
    audit_row,
    encoded,
    event_row,
    identifier,
    item_row,
    row_value,
    session_row,
)
from lucy_api.stream.events import APPROVAL_DENIED, APPROVAL_GRANTED, APPROVAL_REQUESTED

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from lucy_api.sessions.sql_store import SessionStore

PENDING = "pending"
GRANTED = "granted"
TURN_MOVED = "This turn is no longer running."
NEED_APPROVAL_ID = "This input needs an `approval_id`."
NEED_APPROVED = "This input needs `approved` to be true or false."
NOT_WAITING = "This approval is not waiting on an answer."


@dataclass(frozen=True, slots=True)
class Ask:
    """What the model wanted to do, named so a person can answer it."""

    permission: str
    operation: str
    description: str
    arguments: dict[str, Any] = field(default_factory=dict)
    policy: str = "ask"
    termination: str = "input_required"
    stop_reason: str = ""


@dataclass(frozen=True, slots=True)
class Decision:
    """The parked turn, now queued again."""

    turn: dict[str, Any]


async def open_approval(
    store: SessionStore, *, account: str, session_id: str, turn_id: str, ask: Ask
) -> str:
    """Record the ask and park the turn in one commit, so an answer cannot race a claim."""

    def apply(db: sqlite3.Connection) -> str:
        session_row(db, account, session_id)
        parked = db.execute(
            "UPDATE turns SET status='input_required', termination=?, stop_reason=?, "
            "finished_at=NULL WHERE id=? AND status IN ('running','input_required')",
            (ask.termination, ask.stop_reason or None, turn_id),
        )
        if parked.rowcount != 1:
            raise conflict(TURN_MOVED)
        approval_id = identifier("apr")
        now = time.time()
        labelled = ask.operation or ask.permission
        reason = ask.description or labelled
        db.execute(
            "INSERT INTO approvals (id, session_id, turn_id, agent_id, operation, "
            "description, input_json, status, policy, lifetime, instruction, "
            "requested_at, decided_at, decided_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval_id,
                session_id,
                turn_id,
                None,
                labelled,
                reason,
                encoded({"permission": ask.permission, "arguments": ask.arguments}),
                PENDING,
                ask.policy,
                "once",
                None,
                now,
                None,
                None,
            ),
        )
        body = {
            "approval_id": approval_id,
            "tool": labelled,
            "description": reason,
            "arguments": ask.arguments,
            "reason": reason,
            "policy": ask.policy,
            "is_automatic": False,
            "permission": ask.permission,
        }
        item_row(db, session_id, NewItem("approval_request", "assistant", body, turn=turn_id))
        event_row(db, session_id, APPROVAL_REQUESTED, body, turn_id)
        event_row(db, session_id, "lucy.turn.input_required", {"approval_id": approval_id}, turn_id)
        audit_row(
            db,
            account,
            "permission.requested",
            session=session_id,
            turn=turn_id,
            detail={
                "permission": ask.permission,
                "operation": labelled,
                "approval_id": approval_id,
            },
        )
        waiting = db.execute(
            "SELECT 1 FROM turns WHERE session_id=? AND status='queued' LIMIT 1",
            (session_id,),
        ).fetchone()
        db.execute(
            "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
            ("queued" if waiting is not None else "input_required", now, session_id),
        )
        return approval_id

    return await store.transaction(apply)


async def answer_approval(
    store: SessionStore,
    account: str,
    session_id: str,
    event: dict[str, Any],
    key: str,
) -> Decision:
    """Unblock one parked turn. Unknown and already-decided ids are the same miss."""
    approval_id = str(event.get("approval_id") or "")
    if not approval_id:
        raise conflict(NEED_APPROVAL_ID)
    approved = event.get("approved")
    if not isinstance(approved, bool):
        raise conflict(NEED_APPROVED)
    lifetime = str(event.get("lifetime") or "once")
    instruction = str(event.get("instruction") or "")

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        current = session_row(db, account, session_id)
        row = db.execute(
            "SELECT approvals.* FROM approvals JOIN sessions ON sessions.id=approvals.session_id "
            "WHERE approvals.id=? AND approvals.session_id=? AND sessions.account_id=?",
            (approval_id, session_id, account),
        ).fetchone()
        if row is None or str(row["status"]) != PENDING:
            raise absent()
        turn_id = str(row["turn_id"] or "")
        live = db.execute(
            "SELECT status FROM turns WHERE id=? AND session_id=?",
            (turn_id, session_id),
        ).fetchone()
        if live is None or str(live["status"]) != "input_required":
            raise conflict(NOT_WAITING)
        permission = str(_payload(row["input_json"]).get("permission") or row["operation"])
        now = time.time()
        status = GRANTED if approved else "denied"
        db.execute(
            "UPDATE approvals SET status=?, lifetime=?, instruction=?, decided_at=?, "
            "decided_by=? WHERE id=?",
            (status, lifetime, instruction or None, now, account, approval_id),
        )
        audit_row(
            db,
            account,
            "permission.granted" if approved else "permission.denied",
            session=session_id,
            turn=turn_id,
            detail={
                "permission": permission,
                "lifetime": lifetime,
                "approval_id": approval_id,
            },
        )
        waiting = db.execute(
            "SELECT 1 FROM approvals WHERE turn_id=? AND status=? LIMIT 1",
            (turn_id, PENDING),
        ).fetchone()
        if waiting is None:
            db.execute(
                "UPDATE turns SET status='queued', finished_at=NULL, termination=NULL, "
                "stop_reason=NULL WHERE id=?",
                (turn_id,),
            )
            db.execute(
                "UPDATE sessions SET status='queued', updated_at=? WHERE id=?",
                (now, session_id),
            )
        body = {
            "approval_id": approval_id,
            "approved": approved,
            "permission": permission,
            "lifetime": lifetime,
            "instruction": instruction,
        }
        item_row(db, session_id, NewItem("approval_response", "user", body, turn=turn_id))
        event_row(
            db,
            session_id,
            APPROVAL_GRANTED if approved else APPROVAL_DENIED,
            body,
            turn_id,
        )
        if lifetime != "once":
            _store_grant(
                db,
                account,
                Grant(
                    permission=permission,
                    decision="allow" if approved else "deny",
                    profile=_storage_profile(lifetime, str(current["profile"]), session_id),
                    instruction=instruction,
                ),
                now,
            )
        turn = db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return row_value(cast("sqlite3.Row", turn))

    write = IdempotentWrite(
        account=account,
        endpoint=f"/sessions/{session_id}/inputs",
        key=key,
        body={"events": [event]},
        status=HTTPStatus.ACCEPTED,
    )
    return Decision(turn=await store.idempotent(write, apply))


def _payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else {}


def _store_grant(db: sqlite3.Connection, account: str, grant: Grant, now: float) -> None:
    db.execute(
        "INSERT INTO permission_grants "
        "(account_id, profile, permission, decision, instruction, granted_at, source) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id, profile, permission) DO UPDATE SET "
        "decision=excluded.decision, instruction=excluded.instruction, "
        "granted_at=excluded.granted_at, source=excluded.source",
        (
            account,
            grant.profile,
            grant.permission,
            grant.decision,
            grant.instruction,
            now,
            "person",
        ),
    )


def _storage_profile(lifetime: str, profile: str, session_id: str) -> str:
    if lifetime == "account":
        return ACCOUNT_PROFILE
    if lifetime == "session":
        return SESSION_PROFILE_PREFIX + session_id
    return profile


RESUMED_NOTICE = (
    "{operations} {was} approved just now and {has} already run, exactly as approved; "
    "{its} result{s} {are} above. Do not ask for {pronoun} again. Carry on with whatever "
    "depended on {pronoun}; the rest of the plan that asked for {pronoun} did not run."
)
"""What a model is told at the top of a round that a person has just unblocked.

An approved call is run by the hub, with the arguments the person saw, before the model is
asked anything (`approved_calls`, `approved_plan`). It used to be the model's job to ask for
it again "with the same arguments", and two things went wrong. A weak model regenerating a
file rarely reproduces it byte for byte, so the call it re-emitted was not the call the person
approved. And the one-time grant covered the whole permission, so whatever it re-emitted ran
anyway: approved `node test.js`, then found node missing, wrote two files nobody was asked
about and ran `python test.py`, and the person saw none of it.

Phrased as a fact about this turn rather than as an instruction, because it sits in the notice
channel beside the budget warnings, and those are facts too.
"""


@dataclass(frozen=True, slots=True)
class ApprovedCall:
    """One call a person approved and the hub has not run yet."""

    approval_id: str
    operation: str
    arguments: dict[str, Any]


async def approved_calls(store: SessionStore, turn_id: str) -> tuple[ApprovedCall, ...]:
    """The calls approved on this turn that have not run, in the order they were asked.

    Every lifetime, not only "once": whatever else the person allowed, they approved this
    call, and it is this call that runs. Read back rather than carried forward because nothing
    carries it: a parked plan is held only in memory and is gone by the time the answer
    arrives, and the `approvals` row is the one durable record of what the model asked for.
    """

    def read(db: sqlite3.Connection) -> tuple[ApprovedCall, ...]:
        rows = db.execute(
            "SELECT id, operation, input_json FROM approvals "
            "WHERE turn_id=? AND status=? AND executed_at IS NULL "
            "ORDER BY requested_at, rowid",
            (turn_id, GRANTED),
        ).fetchall()
        found: list[ApprovedCall] = []
        for row in rows:
            payload = _payload(row["input_json"])
            arguments = payload.get("arguments")
            found.append(
                ApprovedCall(
                    approval_id=str(row["id"]),
                    operation=str(row["operation"]),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return tuple(found)

    return await store.worker.call(read)


async def mark_executed(store: SessionStore, calls: Sequence[ApprovedCall]) -> None:
    """Record that these calls are being run, before they run.

    Before, not after: a hub that dies mid-call must not run an approved write a second time
    when the turn is claimed again. At most once is the promise an approval card makes.
    """
    if not calls:
        return
    now = time.time()

    def write(db: sqlite3.Connection) -> None:
        db.executemany(
            "UPDATE approvals SET executed_at=? WHERE id=? AND executed_at IS NULL",
            [(now, call.approval_id) for call in calls],
        )

    await store.worker.call(write)


def approved_plan(calls: Sequence[ApprovedCall]) -> dict[str, Any] | None:
    """The approved calls as a plan the executor can run, or nothing to run.

    No `note` on the steps: the executor's plan schema has no such key and refuses the whole
    plan over it. What each call was for is already on its approval record.
    """
    if not calls:
        return None
    return {
        "steps": [
            {"id": f"approved_{index}", "op": call.operation, "input": call.arguments}
            for index, call in enumerate(calls, 1)
        ]
    }


def resumed_notice(operations: Sequence[str]) -> str:
    """One sentence naming what was approved, or nothing when nothing was."""
    if not operations:
        return ""
    names = tuple(dict.fromkeys(operations))
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    single = len(names) == 1
    return RESUMED_NOTICE.format(
        operations=listed,
        was="was" if single else "were",
        has="has" if single else "have",
        its="its" if single else "their",
        s="" if single else "s",
        are="is" if single else "are",
        pronoun="it" if single else "them",
    )


__all__ = [
    "RESUMED_NOTICE",
    "ApprovedCall",
    "Ask",
    "Decision",
    "answer_approval",
    "approved_calls",
    "approved_plan",
    "mark_executed",
    "open_approval",
    "resumed_notice",
]
