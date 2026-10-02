"""Deleting archived conversations once the person's keeping window has passed.

Archiving only hides a conversation. `lucy.delete_archived_sessions_after_days` is for the
person who wants old transcripts actually gone, and deleting cannot be undone, so every
fence here leans towards keeping:

- Zero days, the default and the outage value, deletes nothing.
- A conversation is deleted only when it was archived *and* last touched at least that many
  days ago. Archiving does not stop anybody writing to a conversation, and one somebody
  wrote to yesterday is in use whatever its archive stamp says.
- A conversation with anything still going -- a turn that is not finished, a helper that is
  queued or running, a watch still waiting -- is never deleted.
- One sweep deletes at most `SWEEP_LIMIT`, oldest archive first. A backlog is caught up over
  later listings rather than in one request that takes a minute.

The check and the delete are one transaction, so a conversation unarchived or written to
between them is not deleted on the strength of a reading that is no longer true. Its
artifact files are unlinked after that commit, and only for rows it actually removed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_api.sessions.models import TERMINAL
from lucy_api.sessions.sql_store import audit_row

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore

SWEEP_LIMIT = 10
"""How many conversations one sweep may delete. Each may cost the sandbox a folder delete."""

DAY_SECONDS = 86_400

EXPIRED_ACTION = "session.deleted"
"""The audit action, the same one a person's own delete writes, with the reason beside it."""


KEPT_COLUMNS = ("id", "profile", "workspace_environment_id", "workspace_rel")
"""What the caller needs of a deleted row: whose it was, and where its workspace folder is."""


def _eligible(db: sqlite3.Connection, account: str, cutoff: float, limit: int) -> list[sqlite3.Row]:
    marks = tuple(sorted(TERMINAL))
    placeholders = ",".join("?" for _ in marks)
    rows: list[sqlite3.Row] = db.execute(
        f"""
        SELECT id, profile, workspace_environment_id, workspace_rel
          FROM sessions
         WHERE account_id=?
           AND archived_at IS NOT NULL
           AND archived_at<=?
           AND updated_at<=?
           AND id NOT IN (SELECT session_id FROM turns WHERE status NOT IN ({placeholders}))
           AND id NOT IN (SELECT session_id FROM agents WHERE status IN ('queued','running'))
           AND id NOT IN (
               SELECT session_id FROM subscriptions WHERE state IN ('queued','running')
           )
         ORDER BY archived_at, id
         LIMIT ?
        """,  # noqa: S608 - placeholders are the closed TERMINAL set
        (account, cutoff, cutoff, *marks, limit),
    ).fetchall()
    return rows


async def delete_archived(
    store: SessionStore,
    account: str,
    *,
    days: int,
    now: float,
    limit: int = SWEEP_LIMIT,
) -> tuple[dict[str, Any], ...]:
    """Delete this account's conversations archived and untouched for `days`. Zero is never.

    Returns what went, so the caller can remove each one's workspace folder, which lives in
    the sandbox rather than in this database.
    """
    if days <= 0:
        return ()
    cutoff = now - days * DAY_SECONDS

    def apply(db: sqlite3.Connection) -> tuple[tuple[dict[str, Any], ...], list[str]]:
        gone: list[dict[str, Any]] = []
        files: list[str] = []
        for row in _eligible(db, account, cutoff, limit):
            session = str(row["id"])
            files.extend(
                str(found["path"])
                for found in db.execute(
                    "SELECT path FROM artifacts WHERE session_id=?", (session,)
                ).fetchall()
            )
            audit_row(
                db,
                account,
                EXPIRED_ACTION,
                session=session,
                detail={"reason": "archived_retention", "days": days},
            )
            db.execute("DELETE FROM sessions WHERE id=? AND account_id=?", (session, account))
            gone.append({column: row[column] for column in KEPT_COLUMNS})
        return tuple(gone), files

    expired, files = await store.transaction(apply)
    # After the commit, so a file is only ever gone when its conversation is.
    await store.worker.call(lambda _db: _unlink(files))
    return expired


def _unlink(paths: list[str]) -> None:
    for path in paths:
        Path(path).unlink(missing_ok=True)


__all__ = ["DAY_SECONDS", "KEPT_COLUMNS", "SWEEP_LIMIT", "delete_archived"]
