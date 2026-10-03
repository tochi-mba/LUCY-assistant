"""Compaction as a projection: the transcript stays, a summary stands in for a range.

A compaction that rewrote the log would make "why did it think that?" unanswerable the
moment a summary dropped the detail that explains it. This module only inserts a row.
The prompt assembler projects through active rows; deactivating one restores the turns.

The summary is extractive on purpose. A model-written compaction is a later optimisation
and a new failure mode; identifiers, user sentences and tool names are the MUST-PRESERVE
list the plan names, and they can be copied without asking a provider. Three consecutive
failures switch *automatic* compaction off for the session rather than retrying forever: a
loop that cannot summarise will not summarise on the tenth attempt either. A person can
still compact by hand, and a compaction that works switches the automatic one back on.

Every row says who asked for it -- `manual` (a person, through the API, the CLI or a chat
command) or `auto` (the turn loop, when the window crossed its threshold) -- and how full
the window was when it was asked.
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal

from lucy_api.core.errors import LucyError, absent, conflict
from lucy_api.sessions.sql_store import event_row, identifier, session_row
from lucy_api.stream.events import (
    COMPACTION_APPLIED,
    COMPACTION_DISABLED,
    COMPACTION_FAILED,
    COMPACTION_REVERTED,
    COMPACTION_STARTED,
)

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore

Trigger = Literal["manual", "auto"]

KEEP_RECENT_TURNS = 2
"""Newest turns stay in full. A summary of the turn in progress is a summary of nothing."""

MAX_KEEP_RECENT_TURNS = 100
"""The most a person may ask to keep verbatim. Past it, there is nothing left to compact."""

FAILURES_BEFORE_DISABLE = 3
PROMPT_VERSION = "extractive-v1"
DISABLED_MODEL = "disabled"
DISABLED = (
    "automatic compaction is off for this session after three consecutive failures; "
    "compacting by hand still tries, and switches it back on when it works"
)
EMPTY = "nothing to compact: the session has no items yet"
TOO_NEW = "nothing old enough to compact; the newest {keep} turns stay"
NOTHING_NEW = (
    "nothing new to compact: compaction {seq} already covers everything older than the "
    "newest {keep} turns"
)

PRESERVE = re.compile(
    r"\$[A-Za-z][A-Za-z0-9_]*"
    r"|https?://[^\s\]\"']+"
    r"|/(?:[\w.-]+/)*[\w.-]+\.\w{1,8}"
    r"|\b(?:ses|itm|trn|wrk|env|agt|mem)_[A-Za-z0-9_-]+"
)


async def compact_session(  # noqa: PLR0913 - every argument is a named, defaulted knob
    store: SessionStore,
    account: str,
    session_id: str,
    *,
    model: str = "extractive",
    keep_recent: int = KEEP_RECENT_TURNS,
    trigger: Trigger = "manual",
    trigger_tokens: int = 0,
) -> dict[str, Any]:
    """Write one active compaction covering everything older than the newest turns."""

    keep = max(0, keep_recent)

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        if trigger == "auto" and _disabled(db, session_id):
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
        newest = _newest_active(db, session_id)
        if newest is not None and (newest["covers_from"], newest["covers_to"]) == (
            items[0]["seq"],
            covers_to,
        ):
            # The same range again would be a second row saying what the first one says;
            # with nothing new to cover, a person asking twice is told so instead.
            raise conflict(NOTHING_NEW.format(seq=newest["seq"], keep=keep))
        event_row(db, session_id, COMPACTION_STARTED, {"model": model, "trigger": trigger})
        try:
            summary = _summary(items, covers_to)
            seq = db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM compactions WHERE session_id=?",
                (session_id,),
            ).fetchone()[0]
            compaction_id = identifier("cmp")
            created = time.time()
            db.execute(
                "INSERT INTO compactions "
                "(id,session_id,seq,trigger_tokens,model,prompt_version,summary,"
                "covers_from,covers_to,active,created_at,triggered_by) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    compaction_id,
                    session_id,
                    seq,
                    max(0, trigger_tokens),
                    model,
                    PROMPT_VERSION,
                    summary,
                    items[0]["seq"],
                    covers_to,
                    1,
                    created,
                    trigger,
                ),
            )
            turns = _turns_through(items, covers_to)
            event_row(
                db,
                session_id,
                COMPACTION_APPLIED,
                {
                    "compaction_id": compaction_id,
                    "covers_from": items[0]["seq"],
                    "covers_to": covers_to,
                    "turns": turns,
                    "trigger": trigger,
                    "trigger_tokens": max(0, trigger_tokens),
                },
            )
        except LucyError as exc:
            event_row(db, session_id, COMPACTION_FAILED, {"code": exc.code, "trigger": trigger})
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
            "turns": turns,
            "active": True,
            "trigger": trigger,
            "trigger_tokens": max(0, trigger_tokens),
            "created_at": created,
        }

    written = await store.transaction(apply)
    failure = written.get("_failure")
    if isinstance(failure, LucyError):
        raise failure
    return written


async def uncompact_session(
    store: SessionStore, account: str, session_id: str, compaction_id: str
) -> dict[str, Any]:
    """Deactivate one compaction so the turns it covered are projected again.

    Undoing is an event as much as compacting is: a log that recorded the summary going in
    and not coming out would explain a prompt the model is no longer being sent. Undoing one
    that is already undone changes nothing and records nothing.
    """

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        row = db.execute(
            "SELECT * FROM compactions WHERE id=? AND session_id=? AND model!=?",
            (compaction_id, session_id, DISABLED_MODEL),
        ).fetchone()
        if row is None:
            raise absent()
        if row["active"]:
            db.execute(
                "UPDATE compactions SET active=0 WHERE id=? AND session_id=?",
                (compaction_id, session_id),
            )
            event_row(
                db,
                session_id,
                COMPACTION_REVERTED,
                {
                    "compaction_id": compaction_id,
                    "covers_from": row["covers_from"],
                    "covers_to": row["covers_to"],
                },
            )
        return {
            "id": compaction_id,
            "active": False,
            "covers_from": row["covers_from"],
            "covers_to": row["covers_to"],
        }

    return await store.transaction(apply)


async def list_compactions(store: SessionStore, account: str, session_id: str) -> dict[str, Any]:
    """Every compaction this session has had, newest first, and whether auto is still on.

    `shown` is the one the model is reading: the newest active compaction, and any older
    active one whose range it does not overlap. The rest are active but superseded.
    """

    def read(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        rows = db.execute(
            "SELECT * FROM compactions WHERE session_id=? AND model!=? ORDER BY seq DESC",
            (session_id, DISABLED_MODEL),
        ).fetchall()
        return {"automatic": not _disabled(db, session_id), "data": _public(rows)}

    return await store.transaction(read)


def _public(rows: list[Any]) -> list[dict[str, Any]]:
    taken: list[tuple[int, int]] = []
    listed: list[dict[str, Any]] = []
    for row in rows:
        low, high = int(row["covers_from"]), int(row["covers_to"])
        shown = bool(row["active"]) and not any(
            low <= other_high and other_low <= high for other_low, other_high in taken
        )
        if shown:
            taken.append((low, high))
        listed.append(
            {
                "id": row["id"],
                "seq": row["seq"],
                "active": bool(row["active"]),
                "shown": shown,
                "trigger": row["triggered_by"],
                "trigger_tokens": row["trigger_tokens"],
                "covers_from": low,
                "covers_to": high,
                "model": row["model"],
                "summary": row["summary"],
                "created_at": row["created_at"],
            }
        )
    return listed


def _newest_active(db: sqlite3.Connection, session_id: str) -> Any:
    return db.execute(
        "SELECT seq, covers_from, covers_to FROM compactions "
        "WHERE session_id=? AND active=1 AND model!=? ORDER BY seq DESC LIMIT 1",
        (session_id, DISABLED_MODEL),
    ).fetchone()


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


def _turns_through(items: list[Any], covers_to: int) -> int:
    """How many distinct turns a range ending at `covers_to` reaches into."""
    return len(
        {str(item["turn_id"]) for item in items if item["turn_id"] and item["seq"] <= covers_to}
    )


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


__all__ = [
    "FAILURES_BEFORE_DISABLE",
    "KEEP_RECENT_TURNS",
    "MAX_KEEP_RECENT_TURNS",
    "Trigger",
    "compact_session",
    "list_compactions",
    "uncompact_session",
]
