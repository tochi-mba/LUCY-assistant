"""Opening and closing a turn, and the one word a caller can say while it runs.

A turn is the unit of work a person waits on, so its lifecycle is what a client's spinner is
bound to. Three rules shape everything here.

**A terminal state is final.** ``completed``, ``failed`` and ``cancelled`` are never
overwritten, and the check lives in the database transaction rather than in the loop that
usually does the writing: a process restarted mid-turn replays its recorded steps, and
without immutability the replay would quietly re-finish a turn somebody had already
cancelled.

**Cancelling is cooperative, but only where it has to be.** A turn that is actually running
has a model request in flight and work worth preserving, so a stop sets ``cancel_requested``
and the loop ends the turn itself -- that is the only way progress survives. Every other live
state -- queued, or parked waiting for an approval or a credential -- has nothing in flight to
unwind, so it is ended here and now rather than leaving somebody watching a spinner for a
loop that will not run again until they answer the question they are trying to abandon.

**A stop can only ever hit the turn it names.** The guard is on the turn's own id and its own
status, read and written in one transaction, so a cancel arriving just after a turn finished
is a no-op rather than something that reaches forward and kills its successor. A dropped
connection is not a cancellation at all.

This module is the seam the turn loop plugs into: it opens a turn and it closes one, and it
has no opinion about what happens in between.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, get_args

from lucy_api.core.errors import absent, conflict
from lucy_api.sessions.models import TERMINAL, TurnStatus
from lucy_api.sessions.sql_store import (
    IdempotentWrite,
    NewItem,
    encoded,
    event_row,
    identifier,
    item_row,
    page,
    row_value,
    session_row,
)

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.models import Cursor, Outcome
    from lucy_api.sessions.sql_store import SessionStore

TURN_TABLE = "turns"
QUEUED = "queued"
RUNNING = "running"
CANCELLED = "cancelled"
IDLE = "idle"

STATUSES = frozenset(get_args(TurnStatus))
"""Read off the type rather than restated, so the two cannot drift apart."""

_TERMINAL = tuple(sorted(TERMINAL))
_MARKS = ",".join("?" for _ in _TERMINAL)
# What is interpolated is placeholders, never values: one `?` per terminal state, because a
# hand-written list of three would go stale on the day a fourth terminal state is added.
_LIVE_TURNS = f"SELECT id FROM turns WHERE session_id=? AND status NOT IN ({_MARKS})"  # noqa: S608
_OWNED_TURN = (
    "SELECT turns.* FROM turns JOIN sessions ON sessions.id=turns.session_id "
    "WHERE turns.id=? AND sessions.account_id=?"
)

BUSY = (
    "This session is already working on a turn and its input policy is 'reject'. Wait for "
    "that turn to finish, or set input_policy to 'enqueue' on the session."
)


async def open_turn(
    store: SessionStore, account: str, session: str, events: object
) -> dict[str, Any]:
    """Record a new turn for ``session`` and hand back the row a client will poll.

    The turn starts ``queued``; moving it to ``running`` is the loop's job, because only the
    loop knows when it actually began. ``events`` is stored verbatim as the turn's input, so
    a restart replays what was asked for rather than guessing at it.

    Double-texting is decided here only as far as the data model can decide it. ``reject``
    refuses a second turn outright; ``enqueue``, ``interrupt`` and ``rollback`` all produce a
    queued turn, and what happens to the turn already in flight is the loop's call, since it
    is the only thing holding the half-finished work. The policy in force travels on the
    ``created`` event, so the decision is auditable next to the turn it applied to.
    """

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        current = db.execute(
            "SELECT input_policy FROM sessions WHERE id=? AND account_id=?", (session, account)
        ).fetchone()
        if current is None:
            raise absent()
        live = db.execute(_LIVE_TURNS, (session, *_TERMINAL)).fetchone()
        policy = current["input_policy"]
        if live is not None and policy == "reject":
            raise conflict(BUSY)
        turn = identifier("trn")
        now = time.time()
        db.execute(
            "INSERT INTO turns (id,session_id,status,input_json,created_at) VALUES (?,?,?,?,?)",
            (turn, session, QUEUED, encoded(events), now),
        )
        if live is None:
            # A session's status is the status of the turn it is working on, so a second
            # turn queued behind a running one must not report the session as merely queued.
            db.execute(
                "UPDATE sessions SET status=?,updated_at=? WHERE id=?", (QUEUED, now, session)
            )
        else:
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session))
        event_row(
            db,
            session,
            "lucy.turn.created",
            {"input_policy": policy, "queued_behind": live["id"] if live else None},
            turn,
        )
        return row_value(db.execute("SELECT * FROM turns WHERE id=?", (turn,)).fetchone())

    return await store.transaction(apply)


async def submit_messages(
    store: SessionStore, account: str, session: str, events: list[dict[str, Any]], key: str
) -> dict[str, Any]:
    """Durably accept message input and create its queued turn in one transaction.

    A client retry must never leave a message without a turn, or a turn without the message
    the model is meant to answer. The general input envelope also carries approvals and
    elicitation replies, but those resume a parked turn and are deliberately handled by the
    approval and connection subsystems when they are installed. At this stage only ordinary
    messages can create new work; refusing the other shapes is safer than inventing a new
    turn for an answer that belongs to an existing one.
    """
    if any(event["type"] != "input.message" for event in events):
        detail = "This input needs the approval or connection workflow that owns it."
        raise conflict(detail)

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        current = session_row(db, account, session)
        live = db.execute(_LIVE_TURNS, (session, *_TERMINAL)).fetchone()
        policy = current["input_policy"]
        if live is not None and policy == "reject":
            raise conflict(BUSY)
        turn = identifier("trn")
        now = time.time()
        db.execute(
            "INSERT INTO turns (id,session_id,status,input_json,created_at) VALUES (?,?,?,?,?)",
            (turn, session, QUEUED, encoded({"events": events}), now),
        )
        for event in events:
            item_row(db, session, NewItem("message", "user", event["content"], turn=turn))
        if live is None:
            db.execute(
                "UPDATE sessions SET status=?,updated_at=? WHERE id=?", (QUEUED, now, session)
            )
        else:
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session))
        event_row(
            db,
            session,
            "lucy.turn.created",
            {"input_policy": policy, "queued_behind": live["id"] if live else None},
            turn,
        )
        return row_value(db.execute("SELECT * FROM turns WHERE id=?", (turn,)).fetchone())

    write = IdempotentWrite(
        account=account,
        endpoint=f"/sessions/{session}/inputs",
        key=key,
        body={"events": events},
        status=HTTPStatus.ACCEPTED,
    )
    return await store.idempotent(write, apply)


async def close_turn(
    store: SessionStore, account: str, turn: str, outcome: Outcome
) -> dict[str, Any]:
    """Move ``turn`` to ``outcome.status``, unless it has already reached a terminal one.

    A status outside the vocabulary is a programming error rather than something a caller
    can do, so it raises rather than answering an HTTP status: the loop writing an invented
    state is a bug to fix, not a request to refuse.
    """
    if outcome.status not in STATUSES:
        known = ", ".join(sorted(STATUSES))
        message = f"a turn cannot move to {outcome.status!r}; the statuses are {known}"
        raise ValueError(message)
    await store.finish_turn(account, turn, outcome.status, outcome.termination, outcome.stop_reason)
    return await store.turn(account, turn)


async def cancel_turn(store: SessionStore, account: str, turn: str) -> dict[str, Any]:
    """Ask for ``turn`` to stop, and say what state that left it in.

    Idempotent in the strong sense: asking twice, or asking about a turn that has already
    finished, is not an error and changes nothing. The honest answer to "stop this" when it
    is already stopped is the turn as it stands.
    """

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        row = db.execute(_OWNED_TURN, (turn, account)).fetchone()
        if row is None:
            raise absent()
        if row["status"] in TERMINAL:
            return row_value(row)
        session = row["session_id"]
        db.execute("UPDATE turns SET cancel_requested=1 WHERE id=?", (turn,))
        if row["status"] == RUNNING:
            # Something is in flight. The loop owns ending it, so partial work is kept
            # rather than abandoned half-written.
            event_row(db, session, "lucy.turn.cancel_requested", {"was": RUNNING}, turn)
        else:
            _finish_now(db, session, turn, row["status"])
        return row_value(db.execute("SELECT * FROM turns WHERE id=?", (turn,)).fetchone())

    return await store.transaction(apply)


def _finish_now(db: sqlite3.Connection, session: str, turn: str, was: str) -> None:
    """End a turn with nothing in flight, and hand the session back if it is now free.

    The session goes idle only when no other turn is still live: cancelling something that
    was queued behind a running turn must not tell every client the session stopped working.
    """
    now = time.time()
    db.execute("UPDATE turns SET status=?,finished_at=? WHERE id=?", (CANCELLED, now, turn))
    remaining = db.execute(_LIVE_TURNS + " AND id<>?", (session, *_TERMINAL, turn)).fetchone()
    if remaining is None:
        db.execute("UPDATE sessions SET status=?,updated_at=? WHERE id=?", (IDLE, now, session))
    event_row(db, session, "lucy.turn.cancelled", {"was": was}, turn)


async def list_turns(
    store: SessionStore, account: str, session: str, cursor: Cursor
) -> dict[str, Any]:
    """One cursor page of a session's turns, oldest first unless asked otherwise."""
    rows = await store.records(account, session, TURN_TABLE)
    return page(rows, cursor.limit, cursor.after, cursor.before, cursor.order)
