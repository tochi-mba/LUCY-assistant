"""Transactional, subject-bound session storage; transcripts are never overwritten."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from types import EllipsisType
from typing import TYPE_CHECKING, Any

from lucy_api import __version__
from lucy_api.core.errors import absent, conflict
from lucy_api.sessions.models import TERMINAL, CreateSession
from lucy_api.sessions.schema import SCHEMA

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from lucy_api.store.worker import SqlWorker


IDEMPOTENCY_SECONDS = 3600
RECORD_TABLES = frozenset({"items", "events", "turns", "compactions"})


@dataclass(frozen=True, slots=True)
class NewItem:
    """One entry for the transcript.

    A row is a thing, not six parameters. Naming it stops a call site reading
    `item_row(db, session, "message", "user", body, None, 0)`, where the reader has to
    count commas to find out what `None` was.
    """

    kind: str
    role: str
    content: object
    turn: str | None = None
    tokens: int = 0
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class IdempotentWrite:
    """What makes a retry recognisable as the same request rather than a second one."""

    account: str
    endpoint: str
    key: str
    body: object = None
    status: int = HTTPStatus.OK


def identifier(prefix: str) -> str:
    return prefix + "_" + secrets.token_urlsafe(24)


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def row_value(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    for key in tuple(value):
        if key.endswith("_json"):
            raw = value.pop(key)
            value[key.removesuffix("_json")] = json.loads(raw) if raw is not None else None
    return value


def claimed_one_row(db: sqlite3.Connection) -> bool:
    """Whether the last UPDATE actually took the row.

    The worker serializes writers today, so this is False only in a race a future
    store could lose. Keeping the check here records the invariant rather than
    assuming the SELECT still describes the row we just updated.
    """
    return int(db.execute("SELECT changes()").fetchone()[0]) == 1


def session_row(db: sqlite3.Connection, account: str, session: str) -> sqlite3.Row:
    row: sqlite3.Row | None = db.execute(
        "SELECT * FROM sessions WHERE id=? AND account_id=?", (session, account)
    ).fetchone()
    if row is None:
        raise absent()
    return row


def audit_row(  # noqa: PLR0913 - the row is six facts; collapsing them hides the schema
    db: sqlite3.Connection,
    account: str,
    action: str,
    *,
    session: str | None = None,
    turn: str | None = None,
    detail: object | None = None,
) -> None:
    """Append one security-relevant fact. Never trimmed with the event stream."""
    db.execute(
        "INSERT INTO audit(account_id,session_id,turn_id,agent_id,action,detail_json,at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            account,
            session,
            turn,
            None,
            action,
            encoded(detail or {}),
            time.time(),
        ),
    )


def event_row(
    db: sqlite3.Connection, session: str, kind: str, data: object, turn: str | None = None
) -> dict[str, Any]:
    sequence = db.execute(
        "SELECT COALESCE(MAX(sequence_number),0)+1 FROM events WHERE session_id=?", (session,)
    ).fetchone()[0]
    value = {
        "event_id": identifier("evt"),
        "session_id": session,
        "sequence_number": sequence,
        "type": kind,
        "turn_id": turn,
        "agent_id": None,
        "data": data,
        "created_at": time.time(),
    }
    db.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
        (
            value["event_id"],
            session,
            sequence,
            kind,
            turn,
            None,
            encoded(data),
            value["created_at"],
        ),
    )
    return value


def item_row(
    db: sqlite3.Connection,
    session: str,
    item: NewItem,
    parent: str | EllipsisType | None = ...,
) -> dict[str, Any]:
    """Append one item, chained to ``parent`` or, by default, to the tail of the log.

    Naming a parent explicitly is how edit-and-regenerate writes a *sibling* of an existing
    item rather than its successor: the sequence still climbs, because the log is
    append-only and a number that went backwards would break every cursor over it, but the
    parent chain forks so a reader can tell the two answers apart.

    The default is ``...`` rather than ``None`` because the two mean different things here
    and both are reachable. Omitting the argument asks for the tail; passing ``None`` says
    this item has no parent, which is what regenerating the *first* message in a session
    produces. A single ``None`` default would silently chain that regeneration to the end of
    the conversation it was meant to replace the start of.
    """
    tail = db.execute(
        "SELECT id,seq FROM items WHERE session_id=? ORDER BY seq DESC LIMIT 1", (session,)
    ).fetchone()
    supplied = not isinstance(parent, EllipsisType)
    chained = parent if supplied else (tail["id"] if tail else None)
    value = {
        "id": identifier("itm"),
        "session_id": session,
        "seq": tail["seq"] + 1 if tail else 1,
        "parent_id": chained,
        "turn_id": item.turn,
        "agent_id": item.agent_id,
        "type": item.kind,
        "role": item.role,
        "content": item.content,
        "tokens": item.tokens,
        "created_at": time.time(),
    }
    db.execute(
        "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            value["id"],
            session,
            value["seq"],
            value["parent_id"],
            item.turn,
            item.agent_id,
            item.kind,
            item.role,
            encoded(item.content),
            item.tokens,
            value["created_at"],
        ),
    )
    event_row(db, session, "lucy.content.item.added", value, item.turn)
    return value


class SessionStore:
    """One account predicate at every externally addressable lookup."""

    def __init__(self, worker: SqlWorker) -> None:
        self.worker = worker

    async def initialize(self) -> None:
        await self.worker.call(lambda db: db.executescript(SCHEMA))

    async def healthy(self) -> tuple[bool, str | None]:
        """Whether the database still answers, and the kind of failure when it does not.

        A database that cannot be *created* fails startup, loudly; this is the other case,
        where a process that has been serving for a week finds the file gone, the disk full
        or its worker thread dead. ``/ready`` is unauthenticated, so the reason is the
        exception's *type name* and never its message: a sqlite error routinely carries the
        path of the file it could not open.
        """
        try:
            await self.worker.call(lambda db: db.execute("SELECT 1").fetchone())
        # Deliberately broad: a probe that only caught the failures somebody thought of
        # would report a healthy database while the process could not read a row.
        except Exception as exc:
            return False, type(exc).__name__
        return True, None

    async def transaction[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def apply(db: sqlite3.Connection) -> T:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = operation(db)
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return result

        return await self.worker.call(apply)

    async def idempotent(
        self, write: IdempotentWrite, operation: Callable[[sqlite3.Connection], dict[str, Any]]
    ) -> dict[str, Any]:
        """Store the mutation and its replay response in the same commit.

        Replaying is only safe if the record of the reply is written by the transaction that
        did the work. Two commits would leave a window in which the work is done and a retry
        arriving inside it does the work again.
        """

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            now = time.time()
            db.execute("DELETE FROM idempotency WHERE expires_at < ?", (now,))
            previous = db.execute(
                "SELECT * FROM idempotency WHERE account_id=? AND endpoint=? AND key=?",
                (write.account, write.endpoint, write.key),
            ).fetchone()
            if previous:
                if previous["request_hash"] != digest(write.body):
                    message = "The idempotency key was already used for different input."
                    raise conflict(message)
                replayed: dict[str, Any] = json.loads(previous["response_json"])
                return replayed
            result = operation(db)
            db.execute(
                "INSERT INTO idempotency VALUES (?,?,?,?,?,?,?,?)",
                (
                    write.key,
                    write.account,
                    write.endpoint,
                    digest(write.body),
                    write.status,
                    encoded(result),
                    now,
                    now + IDEMPOTENCY_SECONDS,
                ),
            )
            return result

        return await self.transaction(apply)

    async def create(self, account: str, request: CreateSession, key: str) -> dict[str, Any]:
        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            session = identifier("ses")
            now = time.time()
            db.execute(
                """INSERT INTO sessions (id,account_id,profile,title,status,model,
                thinking_config,persona,harness_version,input_policy,durability_mode,permission_mode,
                incognito,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session,
                    account,
                    request.profile,
                    request.title,
                    "idle",
                    request.model,
                    request.thinking_config,
                    request.persona,
                    __version__,
                    request.input_policy,
                    "durable",
                    request.permission_mode,
                    int(request.incognito),
                    now,
                    now,
                ),
            )
            value = row_value(session_row(db, account, session))
            event_row(db, session, "lucy.session.created", value)
            return value

        write = IdempotentWrite(
            account=account,
            endpoint="/sessions",
            key=key,
            body=request.model_dump(),
            status=HTTPStatus.CREATED,
        )
        return await self.idempotent(write, apply)

    async def get(self, account: str, session: str) -> dict[str, Any]:
        return await self.worker.call(lambda db: row_value(session_row(db, account, session)))

    async def attach_workspace(
        self, account: str, session: str, environment_id: str, workspace_rel: str
    ) -> dict[str, Any]:
        """Attach the first provisioned workspace, once, without replacing one in use."""

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            current = session_row(db, account, session)
            if current["workspace_environment_id"]:
                return row_value(current)
            now = time.time()
            db.execute(
                "UPDATE sessions SET workspace_environment_id=?,workspace_rel=?,updated_at=? "
                "WHERE id=? AND workspace_environment_id IS NULL",
                (environment_id, workspace_rel, now, session),
            )
            value = row_value(session_row(db, account, session))
            event_row(
                db,
                session,
                "lucy.session.workspace_attached",
                {"environment_id": environment_id, "workspace_rel": workspace_rel},
            )
            return value

        return await self.transaction(apply)

    async def claim_next_turn(self) -> dict[str, Any] | None:
        """Claim the oldest runnable queued turn, once, for this process.

        SQLite's writer transaction makes the selection and transition indivisible. A
        session may have queued input behind a running turn, but never two running turns:
        the main loop is the single writer for its conversation. Ids are random, so a
        created_at tie is broken by rowid (insertion order), not by the identifier.
        """

        def apply(db: sqlite3.Connection) -> dict[str, Any] | None:
            row = db.execute(
                "SELECT turns.*,sessions.account_id,sessions.model,sessions.thinking_config "
                "FROM turns JOIN sessions ON sessions.id=turns.session_id "
                "WHERE turns.status='queued' AND NOT EXISTS ("
                "SELECT 1 FROM turns running WHERE running.session_id=turns.session_id "
                "AND running.status='running') ORDER BY turns.created_at, turns.rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            db.execute(
                "UPDATE turns SET status='running',started_at=? WHERE id=? AND status='queued'",
                (now, row["id"]),
            )
            # The update is guarded even though the worker serializes callers: keeping the
            # predicate records the invariant for a future store implementation.
            if not claimed_one_row(db):
                return None
            db.execute(
                "UPDATE sessions SET status='running',updated_at=? WHERE id=?",
                (now, row["session_id"]),
            )
            event_row(db, row["session_id"], "lucy.turn.started", {}, row["id"])
            claimed = db.execute(
                "SELECT turns.*,sessions.account_id,sessions.model,sessions.thinking_config "
                "FROM turns JOIN sessions ON sessions.id=turns.session_id WHERE turns.id=?",
                (row["id"],),
            ).fetchone()
            return row_value(claimed)

        return await self.transaction(apply)

    async def interrupt_abandoned_turns(self) -> tuple[str, ...]:
        """Fail turns a previous process left `running`, so queued work can proceed.

        A crash mid-tool cannot prove the side effect did not already happen, so the turn
        is failed rather than rerun. Parked turns (`input_required`, `auth_required`) wait
        on a person, not this process, and are left alone. Queued turns stay queued.
        """

        def apply(db: sqlite3.Connection) -> tuple[str, ...]:
            rows = db.execute(
                "SELECT turns.id, turns.session_id "
                "FROM turns JOIN sessions ON sessions.id=turns.session_id "
                "WHERE turns.status='running' ORDER BY turns.created_at, turns.id"
            ).fetchall()
            interrupted: list[str] = []
            now = time.time()
            for row in rows:
                turn = str(row["id"])
                session = str(row["session_id"])
                item_row(
                    db,
                    session,
                    NewItem(
                        "error",
                        "assistant",
                        {
                            "code": "process_restarted",
                            "detail": (
                                "This turn was running when Lucy restarted. It was not "
                                "replayed, because a tool that already ran must not run "
                                "again. Send the message again if the work still matters."
                            ),
                        },
                        turn=turn,
                    ),
                )
                db.execute(
                    "UPDATE turns SET status='failed', termination=?, stop_reason=?, "
                    "error_code=?, finished_at=? WHERE id=? AND status='running'",
                    (
                        "error_during_execution",
                        "process_restarted",
                        "process_restarted",
                        now,
                        turn,
                    ),
                )
                event_row(
                    db,
                    session,
                    "lucy.turn.failed",
                    {
                        "termination": "error_during_execution",
                        "stop_reason": "process_restarted",
                    },
                    turn,
                )
                queued = db.execute(
                    "SELECT 1 FROM turns WHERE session_id=? AND status='queued' LIMIT 1",
                    (session,),
                ).fetchone()
                waiting = db.execute(
                    "SELECT status FROM turns WHERE session_id=? "
                    "AND status IN ('input_required','auth_required') LIMIT 1",
                    (session,),
                ).fetchone()
                if queued is not None:
                    session_status = "queued"
                elif waiting is not None:
                    session_status = str(waiting["status"])
                else:
                    session_status = "idle"
                db.execute(
                    "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                    (session_status, now, session),
                )
                interrupted.append(turn)
            return tuple(interrupted)

        return await self.transaction(apply)

    async def stream_snapshot(self, session: str) -> dict[str, Any]:
        """Read the state rendered at the start of an already-authorized event stream.

        The SSE router establishes ownership before subscribing.  The emitter subsequently
        needs a snapshot by opaque session id, rather than another account-shaped public
        lookup; keeping that distinction here prevents a stream helper from becoming an
        alternate externally reachable read path.
        """

        def read(db: sqlite3.Connection) -> dict[str, Any]:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
            if row is None:
                raise absent()
            value = row_value(row)
            latest = db.execute(
                "SELECT id,status,termination,stop_reason FROM turns "
                "WHERE session_id=? ORDER BY started_at DESC,id DESC LIMIT 1",
                (session,),
            ).fetchone()
            value["latest_turn"] = dict(latest) if latest is not None else None
            return value

        return await self.worker.call(read)

    async def list_sessions(
        self, account: str, limit: int, after: str | None, before: str | None, order: str
    ) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            rows = db.execute(
                "SELECT * FROM sessions WHERE account_id=? ORDER BY created_at,id", (account,)
            ).fetchall()
            return page([row_value(row) for row in rows], limit, after, before, order)

        return await self.worker.call(read)

    async def update(self, account: str, session: str, changes: dict[str, Any]) -> dict[str, Any]:
        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            current = session_row(db, account, session)
            for name in ("title", "input_policy", "permission_mode"):
                if changes.get(name) is not None:
                    # `name` comes from the literal tuple above, never from the request.
                    query = f"UPDATE sessions SET {name}=?,updated_at=? WHERE id=?"  # noqa: S608
                    db.execute(query, (changes[name], time.time(), session))
            if changes.get("archived") is not None:
                db.execute(
                    "UPDATE sessions SET archived_at=? WHERE id=?",
                    (time.time() if changes["archived"] else None, session),
                )
            if current["harness_version"] != __version__:
                event_row(
                    db,
                    session,
                    "lucy.session.harness_version_changed",
                    {"previous": current["harness_version"], "current": __version__},
                )
            event_row(db, session, "lucy.session.updated", changes)
            return row_value(session_row(db, account, session))

        return await self.transaction(apply)

    async def delete(self, account: str, session: str) -> None:
        def apply(db: sqlite3.Connection) -> None:
            session_row(db, account, session)
            db.execute(
                "INSERT INTO audit(account_id,session_id,action,detail_json,at) VALUES (?,?,?,?,?)",
                (account, session, "session.deleted", "{}", time.time()),
            )
            db.execute("DELETE FROM sessions WHERE id=?", (session,))

        await self.transaction(apply)

    async def append(self, account: str, session: str, item: NewItem) -> dict[str, Any]:
        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            session_row(db, account, session)
            return item_row(db, session, item)

        return await self.transaction(apply)

    async def event(
        self, account: str, session: str, kind: str, data: object, turn: str | None = None
    ) -> dict[str, Any]:
        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            session_row(db, account, session)
            return event_row(db, session, kind, data, turn)

        return await self.transaction(apply)

    async def records(self, account: str, session: str, table: str) -> list[dict[str, Any]]:
        # Identifiers cannot be SQL parameters. Only authored table names enter SQL.
        if table not in RECORD_TABLES:
            message = f"{table!r} is not a session record table"
            raise ValueError(message)

        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            session_row(db, account, session)
            column = (
                "seq"
                if table in {"items", "compactions"}
                else "sequence_number"
                if table == "events"
                else "created_at"
            )
            # Both names are allowlisted above; an identifier cannot be a SQL parameter.
            query = f"SELECT * FROM {table} WHERE session_id=? ORDER BY {column}"  # noqa: S608
            rows = db.execute(query, (session,)).fetchall()
            return [row_value(row) for row in rows]

        return await self.worker.call(read)

    async def item(self, account: str, item: str) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            row = db.execute(
                "SELECT items.* FROM items JOIN sessions ON sessions.id=items.session_id "
                "WHERE items.id=? AND sessions.account_id=?",
                (item, account),
            ).fetchone()
            if row is None:
                raise absent()
            return row_value(row)

        return await self.worker.call(read)

    async def turn(self, account: str, turn: str) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            row = db.execute(
                "SELECT turns.* FROM turns JOIN sessions ON sessions.id=turns.session_id "
                "WHERE turns.id=? AND sessions.account_id=?",
                (turn, account),
            ).fetchone()
            if row is None:
                raise absent()
            return row_value(row)

        return await self.worker.call(read)

    async def record_audit(
        self,
        account: str,
        action: str,
        *,
        session: str | None = None,
        turn: str | None = None,
        detail: object | None = None,
    ) -> None:
        def apply(db: sqlite3.Connection) -> None:
            if session is not None:
                session_row(db, account, session)
            if turn is not None:
                owned = db.execute(
                    "SELECT turns.id FROM turns JOIN sessions ON sessions.id=turns.session_id "
                    "WHERE turns.id=? AND sessions.account_id=?",
                    (turn, account),
                ).fetchone()
                if owned is None:
                    raise absent()
            audit_row(db, account, action, session=session, turn=turn, detail=detail)

        await self.transaction(apply)

    async def audit_log(self, account: str) -> list[dict[str, Any]]:
        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = db.execute(
                "SELECT * FROM audit WHERE account_id=? ORDER BY sequence",
                (account,),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self.worker.call(read)

    async def finish_turn(
        self,
        account: str,
        turn: str,
        status: str,
        termination: str | None = None,
        stop_reason: str | None = None,
    ) -> None:
        current = await self.turn(account, turn)
        kept_reason = current.get("stop_reason") or stop_reason

        def apply(db: sqlite3.Connection) -> None:
            row = db.execute("SELECT status FROM turns WHERE id=?", (turn,)).fetchone()
            if row is None or row["status"] in TERMINAL:
                return
            db.execute(
                "UPDATE turns SET status=?,termination=?,stop_reason=?,finished_at=? WHERE id=?",
                (
                    status,
                    termination,
                    kept_reason,
                    time.time() if status in TERMINAL else None,
                    turn,
                ),
            )
            queued = db.execute(
                "SELECT 1 FROM turns WHERE session_id=? AND status='queued' LIMIT 1",
                (current["session_id"],),
            ).fetchone()
            session_status = (
                "queued" if queued is not None else "idle" if status in TERMINAL else status
            )
            db.execute(
                "UPDATE sessions SET status=?,updated_at=? WHERE id=?",
                (session_status, time.time(), current["session_id"]),
            )
            event_row(
                db,
                current["session_id"],
                "lucy.turn." + status,
                {"termination": termination, "stop_reason": kept_reason},
                turn,
            )

        await self.transaction(apply)

    async def record_steps(
        self,
        account: str,
        session: str,
        turn_id: str,
        plan: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Persist executed steps so a crash can name what already ran.

        Replay is still refused: a tool that already ran must not run again. The row is
        what a later process reads to explain *why* rather than to continue the plan.
        """
        planned = {
            str(step.get("id")): step
            for step in plan.get("steps") or []
            if isinstance(step, dict) and step.get("id")
        }

        def apply(db: sqlite3.Connection) -> None:
            session_row(db, account, session)
            now = time.time()
            for raw in result.get("steps") or []:
                if not isinstance(raw, dict):
                    continue
                step_id = str(raw.get("id") or "")
                if not step_id:
                    continue
                source = planned.get(step_id, {})
                kind = str(raw.get("op") or raw.get("operation") or source.get("op") or "tool")
                db.execute(
                    "INSERT INTO steps(session_id,turn_id,step_id,kind,status,"
                    "input_digest,result_json,created_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(session_id,turn_id,step_id) DO UPDATE SET "
                    "status=excluded.status, result_json=excluded.result_json",
                    (
                        session,
                        turn_id,
                        step_id,
                        kind,
                        str(raw.get("status") or "ok"),
                        digest(source.get("input", raw.get("input", {}))),
                        encoded(raw),
                        now,
                    ),
                )

        await self.transaction(apply)

    async def steps(self, account: str, session: str, turn_id: str) -> list[dict[str, Any]]:
        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            session_row(db, account, session)
            rows = db.execute(
                "SELECT * FROM steps WHERE session_id=? AND turn_id=? ORDER BY created_at, step_id",
                (session, turn_id),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self.worker.call(read)


def page(
    rows: list[dict[str, Any]], limit: int, after: str | None, before: str | None, order: str
) -> dict[str, Any]:
    """Cursor boundaries refer to identifiers within this already authorized collection."""
    ordered = rows if order == "asc" else list(reversed(rows))
    identifiers = [row["id"] for row in ordered]
    for cursor in (after, before):
        if cursor is not None and cursor not in identifiers:
            raise absent()
    lower = identifiers.index(after) + 1 if after else 0
    upper = identifiers.index(before) if before else len(ordered)
    selected = ordered[lower:upper]
    data = selected[:limit]
    return {
        "data": data,
        "has_more": len(selected) > limit,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }
