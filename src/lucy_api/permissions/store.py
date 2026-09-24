"""The grants ledger: what this person has already answered, keyed per profile.

Settings-api has no profile column, and the question asked here is explicitly per-profile
-- music on the shared profile is not music on the work one. So the ledger lives next to
the session, not next to the catalogue. A missing row is not a denial: it is "not yet
asked", which is why the gate still has a mode.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from lucy_api.core.errors import absent
from lucy_api.permissions.gate import ACCOUNT_PROFILE, Grant, once_key
from lucy_api.sessions.sql_store import audit_row

SESSION_PROFILE_PREFIX = "session:"
"""Grants that last for one conversation. Distinct from the keyring profile name."""

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore


async def grants_for(
    store: SessionStore,
    account: str,
    profile: str,
    *,
    session_id: str = "",
    turn_id: str = "",
) -> dict[str, Grant]:
    """Narrower scopes overlay broader ones. A one-shot answer on this turn wins last."""

    def read(db: sqlite3.Connection) -> dict[str, Grant]:
        wanted = set(_profiles(profile, session_id))
        rows = db.execute(
            "SELECT permission, profile, decision, instruction, source FROM permission_grants "
            "WHERE account_id=? ORDER BY profile",
            (account,),
        ).fetchall()
        found: dict[str, Grant] = {}
        for row in rows:
            if str(row["profile"]) not in wanted:
                continue
            grant = Grant(
                permission=str(row["permission"]),
                decision=str(row["decision"]),
                profile=str(row["profile"]),
                instruction=str(row["instruction"] or ""),
                source=str(row["source"]),
            )
            # ORDER BY profile puts `*` first, then the named profile, then `session:{id}`.
            # Last write wins, so a narrower grant overlays a broader one.
            found[grant.permission] = grant
        if turn_id:
            found.update(_oneshots(db, turn_id, profile))
        return found

    return await store.worker.call(read)


def _profiles(profile: str, session_id: str) -> tuple[str, ...]:
    names = [ACCOUNT_PROFILE, profile]
    if session_id:
        names.append(SESSION_PROFILE_PREFIX + session_id)
    unique: list[str] = []
    for name in names:
        if name not in unique:
            unique.append(name)
    return tuple(unique)


def _oneshots(db: sqlite3.Connection, turn_id: str, profile: str) -> dict[str, Grant]:
    rows = db.execute(
        "SELECT input_json, operation, status, instruction FROM approvals "
        "WHERE turn_id=? AND lifetime='once' AND status IN ('granted','denied') "
        "AND executed_at IS NULL",
        (turn_id,),
    ).fetchall()
    found: dict[str, Grant] = {}
    for row in rows:
        payload = json.loads(row["input_json"]) if row["input_json"] else {}
        raw = payload.get("permission") if isinstance(payload, dict) else None
        permission = str(raw or row["operation"])
        asked = payload.get("arguments") if isinstance(payload, dict) else None
        arguments = asked if isinstance(asked, dict) else {}
        found[once_key(str(row["operation"]), arguments)] = Grant(
            permission=permission,
            decision="allow" if str(row["status"]) == "granted" else "deny",
            profile=profile,
            instruction=str(row["instruction"] or ""),
            source="person",
        )
    return found


async def list_grants(store: SessionStore, account: str) -> list[dict[str, object]]:
    def read(db: sqlite3.Connection) -> list[dict[str, object]]:
        rows = db.execute(
            "SELECT permission, profile, decision, instruction, source, granted_at "
            "FROM permission_grants WHERE account_id=? ORDER BY permission, profile",
            (account,),
        ).fetchall()
        return [dict(row) for row in rows]

    return await store.worker.call(read)


async def put_grant(  # noqa: PLR0913 - the unique key is three columns; collapsing them hides it
    store: SessionStore,
    account: str,
    *,
    permission: str,
    profile: str,
    decision: str,
    instruction: str = "",
    source: str = "person",
) -> dict[str, object]:
    now = time.time()

    def apply(db: sqlite3.Connection) -> dict[str, object]:
        db.execute(
            "INSERT INTO permission_grants "
            "(account_id, profile, permission, decision, instruction, granted_at, source) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id, profile, permission) DO UPDATE SET "
            "decision=excluded.decision, instruction=excluded.instruction, "
            "granted_at=excluded.granted_at, source=excluded.source",
            (account, profile, permission, decision, instruction, now, source),
        )
        audit_row(
            db,
            account,
            "permission.granted" if decision == "allow" else "permission.denied",
            detail={"permission": permission, "profile": profile, "source": source},
        )
        return {
            "permission": permission,
            "profile": profile,
            "decision": decision,
            "instruction": instruction,
            "source": source,
            "granted_at": now,
        }

    return await store.transaction(apply)


async def delete_grant(store: SessionStore, account: str, permission: str, *, profile: str) -> None:
    def apply(db: sqlite3.Connection) -> None:
        cursor = db.execute(
            "DELETE FROM permission_grants WHERE account_id=? AND permission=? AND profile=?",
            (account, permission, profile),
        )
        if cursor.rowcount == 0:
            raise absent()
        audit_row(
            db,
            account,
            "permission.revoked",
            detail={"permission": permission, "profile": profile},
        )

    await store.transaction(apply)


__all__ = ["SESSION_PROFILE_PREFIX", "delete_grant", "grants_for", "list_grants", "put_grant"]
