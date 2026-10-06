"""The grants ledger: what this person has already answered, keyed per profile.

Settings-api has no profile column, and the question asked here is explicitly per-profile
-- music on the shared profile is not music on the work one. So the ledger lives next to
the session, not next to the catalogue. A missing row is not a denial: it is "not yet
asked", which is why the gate still has a mode.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError, absent
from lucy_api.permissions.gate import ACCOUNT_PROFILE, Grant, once_key
from lucy_api.sessions.sql_store import audit_row, row_value

BAD_REQUEST = "bad-request"
ONLY_ALLOWS = (
    "`only` narrows an allow to some of a permission's calls; a deny covers the whole "
    "permission. Send `decision: deny` without `only`."
)

SESSION_PROFILE_PREFIX = "session:"
"""Grants that last for one conversation. Distinct from the keyring profile name."""

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

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
            "SELECT permission, profile, decision, instruction, source, only_json "
            "FROM permission_grants WHERE account_id=? ORDER BY profile",
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
                only=only_of(row["only_json"]),
                wider=found.get(str(row["permission"])),
            )
            # ORDER BY profile puts `*` first, then the named profile, then `session:{id}`.
            # A narrower grant overlays a broader one, and keeps it as `wider` for a call its
            # `only` does not cover.
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
        "WHERE turn_id=? AND status IN ('granted','denied') "
        "AND executed_at IS NULL",
        (turn_id,),
    ).fetchall()
    found: dict[str, Grant] = {}
    for row in rows:
        loaded = json.loads(row["input_json"]) if row["input_json"] else {}
        payload = loaded if isinstance(loaded, dict) else {}
        permission = str(payload.get("permission") or row["operation"])
        # One key per call the card covered, and only those: a card of five helpers
        # answers for those five calls, by their own arguments, and nothing else. Whatever
        # its lifetime: a "yes, for this conversation" is a yes to these calls too, and a
        # call that needs a yes of its own was otherwise asked about again as it replayed.
        for operation, arguments, _stored in calls_of(str(row["operation"]), payload):
            found[once_key(operation, arguments)] = Grant(
                permission=permission,
                decision="allow" if str(row["status"]) == "granted" else "deny",
                profile=profile,
                instruction=str(row["instruction"] or ""),
                source="person",
            )
    return found


def calls_of(
    operation: str, payload: Mapping[str, Any]
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]], ...]:
    """Every call one approval covers: its operation, its arguments, and its stored fields.

    A card of one call is the row itself, as every approval was before cards covered several.
    A card of several lists them; an entry that is not the shape it was written in is
    skipped rather than guessed at, so nothing runs that the card did not show.
    """
    entries = payload.get("calls")
    if not isinstance(entries, list):
        arguments = payload.get("arguments")
        return ((operation, arguments if isinstance(arguments, dict) else {}, dict(payload)),)
    found: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("arguments"), dict):
            continue
        found.append((str(entry.get("operation") or operation), entry["arguments"], entry))
    return tuple(found)


async def list_grants(store: SessionStore, account: str) -> list[dict[str, object]]:
    def read(db: sqlite3.Connection) -> list[dict[str, object]]:
        rows = db.execute(
            "SELECT permission, profile, decision, instruction, source, granted_at, only_json "
            "FROM permission_grants WHERE account_id=? ORDER BY permission, profile",
            (account,),
        ).fetchall()
        return [{**row_value(row), "only": list(only_of(row["only_json"]))} for row in rows]

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
    only: Sequence[str] = (),
) -> dict[str, object]:
    now = time.time()
    grant = Grant(
        permission=permission,
        decision=decision,
        profile=profile,
        instruction=instruction,
        source=source,
        only=tuple(only),
    )

    def apply(db: sqlite3.Connection) -> dict[str, object]:
        kept = upsert_grant(db, account, grant, now)
        audit_row(
            db,
            account,
            "permission.granted" if decision == "allow" else "permission.denied",
            detail={
                "permission": permission,
                "profile": profile,
                "source": source,
                **({"only": list(kept)} if kept else {}),
            },
        )
        return {
            "permission": permission,
            "profile": profile,
            "decision": decision,
            "instruction": instruction,
            "source": source,
            "granted_at": now,
            "only": list(kept),
        }

    return await store.transaction(apply)


def upsert_grant(db: sqlite3.Connection, account: str, grant: Grant, now: float) -> tuple[str, ...]:
    """Record one answer, and return the `only` it leaves in force.

    One row per (account, profile, permission), so a second "always, for this repository" has
    to join the first rather than replace it: both are allows limited to a set, and the set is
    their union. An unrestricted allow clears the limit; a deny replaces whatever was there.

    Raises:
        LucyError: a deny limited to some values. A deny is for the permission, never for a
            few of its calls, because "never, but only for this one" leaves the rest unasked.
    """
    if grant.decision != "allow" and grant.only:
        raise LucyError(BAD_REQUEST, ONLY_ALLOWS, 400)
    only = tuple(dict.fromkeys(value.strip() for value in grant.only if value.strip()))
    existing = db.execute(
        "SELECT decision, only_json FROM permission_grants "
        "WHERE account_id=? AND profile=? AND permission=?",
        (account, grant.profile, grant.permission),
    ).fetchone()
    if existing is not None and only and str(existing["decision"]) == "allow":
        before = only_of(existing["only_json"])
        only = tuple(dict.fromkeys((*before, *only))) if before else ()
    db.execute(
        "INSERT INTO permission_grants "
        "(account_id, profile, permission, decision, instruction, granted_at, source, only_json) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account_id, profile, permission) DO UPDATE SET "
        "decision=excluded.decision, instruction=excluded.instruction, "
        "granted_at=excluded.granted_at, source=excluded.source, only_json=excluded.only_json",
        (
            account,
            grant.profile,
            grant.permission,
            grant.decision,
            grant.instruction,
            now,
            grant.source,
            json.dumps(list(only)) if only else None,
        ),
    )
    return only


def only_of(raw: object) -> tuple[str, ...]:
    """The stored `only`, or nothing. A value that is not a list of strings limits nothing."""
    if not isinstance(raw, str) or not raw:
        return ()
    loaded = json.loads(raw)
    if not isinstance(loaded, list):
        return ()
    return tuple(str(value) for value in loaded if isinstance(value, str) and value)


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


__all__ = [
    "ONLY_ALLOWS",
    "SESSION_PROFILE_PREFIX",
    "calls_of",
    "delete_grant",
    "grants_for",
    "list_grants",
    "only_of",
    "put_grant",
    "upsert_grant",
]
