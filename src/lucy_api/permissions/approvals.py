"""Parking a turn on a person, and resuming it from their answer.

The gate decides that a write may not run yet. This module is the other half: a durable
approval row, an item a client can show, and the one input event that unblocks the turn.
The client's `approved: true` is an input, not an authorization — the grant is recorded
here, and the next claim of the turn re-runs the gate against it.

One card per permission per plan. A plan that starts five helpers used to put five cards in
front of the person, each asking the same question about one call. The calls a plan parks
under one permission are now one card that names the permission once and lists every call
(`cards`). Answered "yes, once", it approves exactly those calls, each by its own arguments,
and each replays with the steps it reads from; nothing else, and no later plan. Different
permissions stay different cards, because they are different questions.
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
from lucy_api.permissions.replay import needs as needs_of_plan
from lucy_api.permissions.store import SESSION_PROFILE_PREFIX, calls_of, upsert_grant
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
    from collections.abc import Mapping, Sequence

    from lucy_api.permissions.replay import Needs
    from lucy_api.sessions.sql_store import SessionStore

PENDING = "pending"
GRANTED = "granted"
TURN_MOVED = "This turn is no longer running."
NEED_APPROVAL_ID = "This input needs an `approval_id`."
NEED_APPROVED = "This input needs `approved` to be true or false."
NOT_WAITING = "This approval is not waiting on an answer."
ONLY_NEEDS = (
    "`only` limits a standing yes to some calls: it needs `approved: true` and a `lifetime` of "
    "session, profile or account."
)


@dataclass(frozen=True, slots=True)
class AskedCall:
    """One call on a card that asks about several of one permission."""

    operation: str
    arguments: dict[str, Any]
    description: str = ""
    needs: Needs | None = None


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
    needs: Needs | None = None
    """What the call needs from its plan to run as planned (:mod:`.replay`); none recorded
    for an ask that came without a plan step."""
    calls: tuple[AskedCall, ...] = ()
    """Every call this card covers, when it covers more than one; each is approved, recorded
    and replayed by its own arguments. Empty for the card that asks about one call."""
    limit_field: str = ""
    """The permission's `tally` field, when a standing yes may be limited by it."""
    limit_values: tuple[str, ...] = ()
    """The whole values of that field across the calls on the card, first seen first: what
    "always, for this repository" would be limited to."""


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
                encoded(
                    {
                        "permission": ask.permission,
                        "arguments": ask.arguments,
                        **_needs_payload(ask.needs),
                        **_calls_payload(ask.calls),
                    }
                ),
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
        if ask.limit_field:
            # A client offering "always, for this repository" needs the field and the
            # whole values, not the cut labels the sentence counts by.
            body["limit"] = {"field": ask.limit_field, "values": list(ask.limit_values)}
        if ask.calls:
            body["count"] = len(ask.calls)
            body["steps"] = [
                {
                    "step": call.needs.step if call.needs is not None else "",
                    "operation": call.operation,
                    "arguments": call.arguments,
                    "description": call.description,
                }
                for call in ask.calls
            ]
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
    only = tuple(str(value) for value in event.get("only") or ())
    if only and (lifetime == "once" or not approved):
        raise conflict(ONLY_NEEDS)

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
                **({"only": list(only)} if only else {}),
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
            **({"only": list(only)} if only else {}),
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
            upsert_grant(
                db,
                account,
                Grant(
                    permission=permission,
                    decision="allow" if approved else "deny",
                    profile=_storage_profile(lifetime, str(current["profile"]), session_id),
                    instruction=instruction,
                    only=only,
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


def _storage_profile(lifetime: str, profile: str, session_id: str) -> str:
    if lifetime == "account":
        return ACCOUNT_PROFILE
    if lifetime == "session":
        return SESSION_PROFILE_PREFIX + session_id
    return profile


RESUMED_NOTICE = (
    "{operations} {was} approved just now and {has} already run, exactly as approved, with "
    "the steps {they} read from; {its} result{s} {are} above, including any failure. Do not "
    "ask for {pronoun} again. Carry on with whatever depended on {pronoun}; the rest of the "
    "plan that asked for {pronoun} did not run."
)
"""What a model is told at the top of a round that a person has just unblocked.

An approved call is run by the hub, with the arguments the person saw, before the model is
asked anything (`approved_calls`, :func:`.replay.replay`). It used to be the model's job to ask for
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
    step: str = ""
    """The call's step id in the plan that asked for it; empty for an approval recorded before
    approvals kept it, which runs on its own as it always did."""
    needs: tuple[dict[str, Any], ...] = ()
    """The call's step and every step it reads from, in plan order."""
    gated: tuple[str, ...] = ()
    """Steps among ``needs`` that were parked beside it, and so must have been approved too."""
    plan: str = ""
    """A digest of the plan it came from."""


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
            found.extend(
                ApprovedCall(
                    approval_id=str(row["id"]),
                    operation=operation,
                    arguments=arguments,
                    **_needs_of(each),
                )
                for operation, arguments, each in calls_of(str(row["operation"]), payload)
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


async def reopen(
    store: SessionStore,
    calls: Sequence[ApprovedCall],
    plan: Mapping[str, Any] | None,
    *,
    parked: Sequence[str],
) -> None:
    """Give back approved calls whose replay parked again, recorded against the plan that did.

    The gate parks a plan before any step runs, so an approved call in a plan that parked
    has not run and its approval is not spent. Each is recorded again with what it now needs
    from that plan, and which of those steps were parked beside it, so it is replayed
    together with whatever the person answers next -- or held, if they refuse.
    """
    rewritten = {
        (call.approval_id, call.step): _needs_payload(needs_of_plan(plan, call.step, parked=parked))
        for call in calls
    }
    rows = tuple(dict.fromkeys(call.approval_id for call in calls))

    def write(db: sqlite3.Connection) -> None:
        for approval_id in rows:
            row = db.execute(
                "SELECT input_json FROM approvals WHERE id=?", (approval_id,)
            ).fetchone()
            payload = _payload(row["input_json"]) if row is not None else {}
            entries = payload.get("calls")
            if isinstance(entries, list):
                # A card of several calls: each is recorded again by its own step.
                for entry in entries:
                    step = str(entry.get("step") or "") if isinstance(entry, dict) else ""
                    if (approval_id, step) in rewritten:
                        entry.update(rewritten[(approval_id, step)])
            else:
                step = str(payload.get("step") or "")
                payload.update(rewritten.get((approval_id, step), {}))
            db.execute(
                "UPDATE approvals SET executed_at=NULL, input_json=? WHERE id=?",
                (json.dumps(payload), approval_id),
            )

    await store.worker.call(write)


def _needs_payload(needs: Needs | None) -> dict[str, Any]:
    """The replay fields of an approval's stored input, or none for an ask with no step."""
    if needs is None or not needs.step:
        return {}
    return {
        "step": needs.step,
        "needs": list(needs.steps),
        "gated": list(needs.gated),
        "plan": needs.plan,
    }


def _calls_payload(calls: Sequence[AskedCall]) -> dict[str, Any]:
    """The stored calls of a card that covers several, or nothing for a card of one."""
    if not calls:
        return {}
    return {
        "calls": [
            {
                "operation": call.operation,
                "arguments": call.arguments,
                "description": call.description,
                **_needs_payload(call.needs),
            }
            for call in calls
        ]
    }


def cards(asks: Sequence[Mapping[str, Any]]) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    """The asks of one parked plan, one card per permission, in the order first asked."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for ask in asks:
        grouped.setdefault(str(ask.get("permission") or ""), []).append(ask)
    return tuple(tuple(group) for group in grouped.values())


def card_sentence(asks: Sequence[Mapping[str, Any]]) -> str:
    """What a card of several calls says: the permission once, and how many of each.

    "Start a helper, 5 calls in this plan: researcher x2, reviewer x3". Counted by the
    permission's tally when it has one, so the person reads a team, not five sentences.
    """
    first = asks[0]
    title = str(first.get("title") or first.get("permission") or first.get("operation") or "")
    labels = [str(ask.get("label") or "") for ask in asks]
    head = f"{title}, {len(asks)} calls in this plan"
    if not all(labels):
        return head
    counted = ", ".join(f"{label} x{labels.count(label)}" for label in dict.fromkeys(labels))
    return f"{head}: {counted}"


def _needs_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The replay fields back, dropping anything that is not the shape they were written in.

    A row whose fields cannot be read runs its call on its own, which is what every approval
    did before they were recorded: the call the person saw, with nothing added to it.
    """
    step = payload.get("step")
    steps = payload.get("needs")
    gated = payload.get("gated")
    plan = payload.get("plan")
    if not isinstance(step, str) or not step or not isinstance(plan, str):
        return {}
    if not isinstance(steps, list) or not all(_is_step(item) for item in steps):
        return {}
    if not isinstance(gated, list) or not all(isinstance(item, str) for item in gated):
        return {}
    return {"step": step, "needs": tuple(steps), "gated": tuple(gated), "plan": plan}


def _is_step(item: object) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("op"), str)
        and isinstance(item.get("input"), dict)
    )


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
        they="it" if single else "they",
    )


__all__ = [
    "RESUMED_NOTICE",
    "ApprovedCall",
    "Ask",
    "AskedCall",
    "Decision",
    "answer_approval",
    "approved_calls",
    "card_sentence",
    "cards",
    "mark_executed",
    "open_approval",
    "reopen",
    "resumed_notice",
]
