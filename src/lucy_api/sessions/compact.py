"""Compaction as a projection: the transcript stays, a summary stands in for a range.

A compaction that rewrote the log would make "why did it think that?" unanswerable the
moment a summary dropped the detail that explains it. This module only inserts a row.
The prompt assembler projects through active rows; deactivating one restores the turns.

The summary is extractive on purpose. A model-written compaction is a later optimisation
and a new failure mode; identifiers, user sentences and tool names are the MUST-PRESERVE
list the plan names, and they can be copied without asking a provider. Three consecutive
failures disable compaction for the session rather than retrying forever: a loop that
cannot summarise will not summarise on the tenth attempt either.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError, absent, conflict
from lucy_api.sessions.sql_store import event_row, identifier, session_row
from lucy_api.stream.events import (
    COMPACTION_APPLIED,
    COMPACTION_DISABLED,
    COMPACTION_FAILED,
    COMPACTION_STARTED,
)

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore

KEEP_RECENT_TURNS = 2
"""Newest turns stay in full. A summary of the turn in progress is a summary of nothing."""

FAILURES_BEFORE_DISABLE = 3
PROMPT_VERSION = "extractive-v1"
DISABLED_MODEL = "disabled"
DISABLED = "compaction is disabled for this session after three consecutive failures"
EMPTY = "nothing to compact: the session has no items yet"
TOO_NEW = "nothing old enough to compact; the newest {keep} turns stay"

PRESERVE = re.compile(
    r"\$[A-Za-z][A-Za-z0-9_]*"
    r"|https?://[^\s\]\"']+"
    r"|/(?:[\w.-]+/)*[\w.-]+\.\w{1,8}"
    r"|\b(?:ses|itm|trn|wrk|env|agt|mem)_[A-Za-z0-9_-]+"
)


async def compact_session(
    store: SessionStore,
    account: str,
    session_id: str,
    *,
    model: str = "extractive",
    keep_recent: int = KEEP_RECENT_TURNS,
) -> dict[str, Any]:
    """Write one active compaction covering everything older than the newest turns."""

    keep = max(0, keep_recent)

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        if _disabled(db, session_id):
            raise conflict(DISABLED)
        items = db.execute(
            "SELECT seq, turn_id, role, type, content_json FROM items "
            "WHERE session_id=? ORDER BY seq",
            (session_id,),
        ).fetchall()
        if not items:
            raise conflict(EMPTY)
        covers_to = _covers_to(items, keep)
        if covers_to is None:
            raise conflict(TOO_NEW.format(keep=keep))
        event_row(db, session_id, COMPACTION_STARTED, {"model": model})
        try:
            summary = _summary(items, covers_to)
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM compactions WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            compaction_id = identifier("cmp")
            db.execute(
                "INSERT INTO compactions "
                "(id,session_id,seq,trigger_tokens,model,prompt_version,summary,"
                "covers_from,covers_to,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    compaction_id,
                    session_id,
                    seq,
                    0,
                    model,
                    PROMPT_VERSION,
                    summary,
                    items[0]["seq"],
                    covers_to,
                    1,
                    time.time(),
                ),
            )
            event_row(
                db,
                session_id,
                COMPACTION_APPLIED,
                {"compaction_id": compaction_id, "covers_to": covers_to},
            )
        except LucyError as exc:
            event_row(db, session_id, COMPACTION_FAILED, {"code": exc.code})
            if _consecutive_failures(db, session_id) >= FAILURES_BEFORE_DISABLE:
                db.execute(
                    "INSERT INTO compactions "
                    "(id,session_id,seq,trigger_tokens,model,prompt_version,summary,"
                    "covers_from,covers_to,active,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier("cmp"),
                        session_id,
                        0,
                        0,
                        DISABLED_MODEL,
                        PROMPT_VERSION,
                        "compaction disabled after three consecutive failures",
                        0,
                        0,
                        0,
                        time.time(),
                    ),
                )
                event_row(
                    db,
                    session_id,
                    COMPACTION_DISABLED,
                    {"failures": FAILURES_BEFORE_DISABLE},
                )
            return {"_failure": exc}
        return {
            "id": compaction_id,
            "seq": seq,
            "summary": summary,
            "covers_from": items[0]["seq"],
            "covers_to": covers_to,
            "active": True,
        }

    written = await store.transaction(apply)
    failure = written.get("_failure")
    if isinstance(failure, LucyError):
        raise failure
    return written


async def uncompact_session(
    store: SessionStore, account: str, session_id: str, compaction_id: str
) -> dict[str, Any]:
    """Deactivate one compaction so the turns it covered are projected again."""

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        row = db.execute(
            "SELECT * FROM compactions WHERE id=? AND session_id=?",
            (compaction_id, session_id),
        ).fetchone()
        if row is None:
            raise absent()
        db.execute(
            "UPDATE compactions SET active=0 WHERE id=? AND session_id=?",
            (compaction_id, session_id),
        )
        return {
            "id": compaction_id,
            "active": False,
            "covers_from": row["covers_from"],
            "covers_to": row["covers_to"],
        }

    return await store.transaction(apply)


def _disabled(db: sqlite3.Connection, session_id: str) -> bool:
    row = db.execute(
        "SELECT type FROM events WHERE session_id=? AND type IN (?,?,?) "
        "ORDER BY sequence_number DESC LIMIT 1",
        (session_id, COMPACTION_DISABLED, COMPACTION_APPLIED, COMPACTION_FAILED),
    ).fetchone()
    return row is not None and row["type"] == COMPACTION_DISABLED


def _consecutive_failures(db: sqlite3.Connection, session_id: str) -> int:
    rows = db.execute(
        "SELECT type FROM events WHERE session_id=? AND type IN (?,?,?) "
        "ORDER BY sequence_number DESC",
        (session_id, COMPACTION_FAILED, COMPACTION_APPLIED, COMPACTION_DISABLED),
    ).fetchall()
    count = 0
    for row in rows:
        if row["type"] == COMPACTION_APPLIED:
            return count
        if row["type"] == COMPACTION_FAILED:
            count += 1
    return count


def _covers_to(items: list[Any], keep_recent: int = KEEP_RECENT_TURNS) -> int | None:
    keep = max(0, keep_recent)
    turns: list[str] = []
    for item in items:
        turn = item["turn_id"]
        if turn and str(turn) not in turns:
            turns.append(str(turn))
    if keep == 0:
        last = 0
        for item in items:
            last = int(item["seq"])
        return last or None
    if len(turns) <= keep:
        return None
    kept = set(turns[-keep:])
    last = 0
    locked = False
    for item in items:
        turn = item["turn_id"]
        if locked:
            continue
        if turn and str(turn) in kept:
            locked = True
            continue
        last = int(item["seq"])
    return last or None


def _summary(items: list[Any], covers_to: int) -> str:
    identifiers: list[str] = []
    seen: set[str] = set()
    users: list[str] = []
    tools: list[str] = []
    for item in items:
        if int(item["seq"]) > covers_to:
            break
        text = _text(item["content_json"])
        for match in PRESERVE.findall(text):
            if match not in seen:
                seen.add(match)
                identifiers.append(match)
        if item["role"] == "user":
            users.append(_clip(text, 200))
        operation = _operation(text)
        if operation and operation not in tools:
            tools.append(operation)
    lines = [
        "MUST-PRESERVE: identifiers, user requests and tools already used.",
        "Identifiers: " + (", ".join(identifiers) if identifiers else "(none)"),
        "Tools: " + (", ".join(tools) if tools else "(none)"),
    ]
    if users:
        lines.append("User: " + " | ".join(users[:8]))
    lines.append(f"Covered items 1-{covers_to}.")
    return "\n".join(lines)


def _text(raw: object) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        raw = loaded
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    return str(raw)


def _operation(text: str) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("op") or payload.get("operation") or payload.get("name")
    return str(value) if value else ""


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


__all__ = ["FAILURES_BEFORE_DISABLE", "KEEP_RECENT_TURNS", "compact_session", "uncompact_session"]
