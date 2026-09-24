"""Durable helper rows, mail and the journal blackboard.

The work registry is process memory: it dies on restart. These tables are how a restarted
process knows which helpers were running, which messages were waiting, and which journal
tasks a dead claimant had locked. Every lookup that names a person's data carries the
account, and a miss is `absent()` — the same 404-not-403 rule as the session store.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lucy_api.context.types import TaskSnapshot
from lucy_api.core.errors import LucyError, absent, conflict
from lucy_api.sessions.sql_store import encoded, identifier, row_value, session_row

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore

LEASE_SECONDS = 120.0
"""How long a journal claim lasts without a heartbeat.

A dead helper must not hold a task forever. Two minutes is long enough for a round-trip
and short enough that a crash is visible on the next gather.
"""

MAIL_MAX_CHARS = 4000
"""A steer is a sentence, not a transcript. Anything longer belongs in the child's items."""

MAIL_BURST = 8
"""Undelivered messages a helper may hold. The parent that keeps steering is the failure."""

MAIL_MAX_HOPS = 4
"""A loop through three helpers must terminate. The fifth copy is refused, not delivered."""

MAIL_TOO_FAR = "mail-too-far"
MAIL_TOO_LONG = "mail-too-long"
MAIL_TOO_MANY = "mail-too-many"
TASK_FINISHED = "that task is already finished"
TASK_HELD = "that task is already claimed; wait for the lease to expire"


@dataclass(frozen=True, slots=True)
class Interrupted:
    """A helper a previous process was running when it stopped, and whose it was."""

    id: str
    account_id: str
    session_id: str
    role: str
    objective: str
    depth: int
    started_at: float
    last_seen: float
    """When it last wrote to its transcript: the nearest thing on record to when it died."""


class AgentStore:
    """One account predicate on every helper, every message and every journal row."""

    def __init__(self, sessions: SessionStore) -> None:
        self._sessions = sessions

    async def insert(  # noqa: PLR0913 - the row is identity, brief and depth
        self,
        account: str,
        session: str,
        *,
        role: str,
        objective: str,
        depth: int,
        parent_agent_id: str | None = None,
        delegation: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> str:
        agent = agent_id or identifier("agt")
        payload = dict(delegation or {})

        def apply(db: sqlite3.Connection) -> str:
            session_row(db, account, session)
            now = time.time()
            db.execute(
                "INSERT INTO agents(id,session_id,parent_agent_id,role,objective,"
                "delegation_json,status,depth,tools_json,budget_json,workspace_rel,"
                "progress,result_json,summary_tokens,interrupted_reason,created_at,"
                "started_at,finished_at,input_tokens,output_tokens,cost_micros) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    agent,
                    session,
                    parent_agent_id,
                    role,
                    objective,
                    encoded(payload),
                    "running",
                    depth,
                    encoded([]),
                    encoded({}),
                    None,
                    "",
                    None,
                    0,
                    None,
                    now,
                    now,
                    None,
                    0,
                    0,
                    0,
                ),
            )
            return agent

        return await self._sessions.transaction(apply)

    async def get(self, account: str, agent_id: str) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            return row_value(_owned_agent(db, account, agent_id))

        return await self._sessions.worker.call(read)

    async def running(self, account: str, session: str) -> list[dict[str, Any]]:
        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            session_row(db, account, session)
            rows = db.execute(
                "SELECT agents.* FROM agents JOIN sessions ON sessions.id=agents.session_id "
                "WHERE agents.session_id=? AND sessions.account_id=? AND agents.status='running' "
                "ORDER BY agents.created_at, agents.id",
                (session, account),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self._sessions.worker.call(read)

    async def for_session(self, account: str, session: str) -> list[dict[str, Any]]:
        """Every helper this conversation started, running or finished."""

        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            session_row(db, account, session)
            rows = db.execute(
                "SELECT agents.* FROM agents JOIN sessions ON sessions.id=agents.session_id "
                "WHERE agents.session_id=? AND sessions.account_id=? "
                "ORDER BY agents.created_at, agents.id",
                (session, account),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self._sessions.worker.call(read)

    async def finish(  # noqa: PLR0913 - status, result and tokens are one write
        self,
        account: str,
        agent_id: str,
        *,
        status: str,
        result: object | None = None,
        summary_tokens: int = 0,
        interrupted_reason: str | None = None,
    ) -> None:
        def apply(db: sqlite3.Connection) -> None:
            _owned_agent(db, account, agent_id)
            db.execute(
                "UPDATE agents SET status=?, result_json=?, summary_tokens=?, "
                "interrupted_reason=?, finished_at=? WHERE id=?",
                (
                    status,
                    encoded(result) if result is not None else None,
                    summary_tokens,
                    interrupted_reason,
                    time.time(),
                    agent_id,
                ),
            )

        await self._sessions.transaction(apply)

    async def interrupt_running(self) -> tuple[Interrupted, ...]:
        """Mark every in-process helper dead, and say whose each one was.

        A restarted process has lost the child's event loop, so they cannot be resurrected
        from memory. Pretending the helper is still running would make the parent wait on a
        notice that will never arrive. Claimed journal tasks go back to pending so another
        helper can take them.
        """

        def apply(db: sqlite3.Connection) -> tuple[Interrupted, ...]:
            rows = db.execute(
                "SELECT agents.id, sessions.account_id, agents.session_id, agents.role, "
                "agents.objective, agents.depth, agents.created_at, "
                "(SELECT MAX(items.created_at) FROM items WHERE items.agent_id=agents.id) "
                "AS last_seen "
                "FROM agents JOIN sessions ON sessions.id=agents.session_id "
                "WHERE agents.status='running' ORDER BY agents.id"
            ).fetchall()
            now = time.time()
            db.execute(
                "UPDATE agents SET status='interrupted', interrupted_reason=?, finished_at=? "
                "WHERE status='running'",
                ("process_restarted", now),
            )
            db.execute(
                "UPDATE journal SET status='pending', claimed_by=NULL, lease_until=NULL "
                "WHERE status='in_progress'"
            )
            return tuple(
                Interrupted(
                    id=str(row["id"]),
                    account_id=str(row["account_id"]),
                    session_id=str(row["session_id"]),
                    role=str(row["role"]),
                    objective=str(row["objective"]),
                    depth=int(row["depth"]),
                    started_at=float(row["created_at"]),
                    last_seen=float(row["last_seen"] or row["created_at"]),
                )
                for row in rows
            )

        return await self._sessions.transaction(apply)

    async def send_mail(  # noqa: PLR0913 - caps are the person's; hops and sender are routing
        self,
        account: str,
        agent_id: str,
        body: str,
        *,
        sender: str = "parent",
        hops: int = 0,
        max_chars: int | None = None,
        burst: int | None = None,
    ) -> str:
        mail = identifier("msg")
        text = body.strip()
        limit = MAIL_MAX_CHARS if max_chars is None else max_chars
        burst_limit = MAIL_BURST if burst is None else burst

        def apply(db: sqlite3.Connection) -> str:
            _owned_agent(db, account, agent_id)
            if not text:
                return mail
            if hops > MAIL_MAX_HOPS:
                too_far = f"a message may hop {MAIL_MAX_HOPS} times, not {hops}"
                raise LucyError(MAIL_TOO_FAR, too_far, 409)
            if len(text) > limit:
                too_long = f"a steer may be {limit} characters, not {len(text)}"
                raise LucyError(MAIL_TOO_LONG, too_long, 409)
            waiting = db.execute(
                "SELECT id, sender, body FROM agent_mail "
                "WHERE agent_id=? AND delivered_at IS NULL ORDER BY created_at, id",
                (agent_id,),
            ).fetchall()
            if (
                waiting
                and str(waiting[-1]["sender"]) == sender
                and str(waiting[-1]["body"]) == text
            ):
                return str(waiting[-1]["id"])
            if len(waiting) >= burst_limit:
                too_many = (
                    f"that helper already has {burst_limit} unread messages; wait for it to drain"
                )
                raise LucyError(MAIL_TOO_MANY, too_many, 409)
            db.execute(
                "INSERT INTO agent_mail(id,agent_id,direction,sender,body,hops,"
                "created_at,delivered_at) VALUES (?,?,?,?,?,?,?,?)",
                (mail, agent_id, "in", sender, text, hops, time.time(), None),
            )
            return mail

        return await self._sessions.transaction(apply)

    async def drain_mail(self, account: str, agent_id: str) -> tuple[str, ...]:
        """Unread parent messages, in order, marked delivered. Empty means nothing waiting."""

        def apply(db: sqlite3.Connection) -> tuple[str, ...]:
            _owned_agent(db, account, agent_id)
            rows = db.execute(
                "SELECT id, body FROM agent_mail WHERE agent_id=? AND delivered_at IS NULL "
                "ORDER BY created_at, id",
                (agent_id,),
            ).fetchall()
            now = time.time()
            bodies: list[str] = []
            for row in rows:
                db.execute(
                    "UPDATE agent_mail SET delivered_at=? WHERE id=?",
                    (now, row["id"]),
                )
                bodies.append(str(row["body"]))
            return tuple(bodies)

        return await self._sessions.transaction(apply)

    async def add_task(  # noqa: PLR0913 - the journal row is title, owner and edges
        self,
        account: str,
        session: str,
        *,
        title: str,
        agent_id: str | None = None,
        status: str = "in_progress",
        depends_on: str = "",
    ) -> int:
        def apply(db: sqlite3.Connection) -> int:
            session_row(db, account, session)
            now = time.time()
            claimed = agent_id if status == "in_progress" else None
            lease = now + LEASE_SECONDS if claimed else None
            cursor = db.execute(
                "INSERT INTO journal(session_id,agent_id,kind,title,status,depends_on,"
                "claimed_by,lease_until,detail_json,at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    session,
                    agent_id,
                    "research",
                    title,
                    status,
                    depends_on,
                    claimed,
                    lease,
                    encoded({}),
                    now,
                ),
            )
            return int(cursor.lastrowid or 0)

        return await self._sessions.transaction(apply)

    async def claim_task(self, account: str, session: str, task_id: int, *, agent_id: str) -> None:
        """Take one open journal row, or renew a claim this helper already holds."""

        def apply(db: sqlite3.Connection) -> None:
            session_row(db, account, session)
            row = db.execute(
                "SELECT id, status, claimed_by, lease_until FROM journal "
                "WHERE id=? AND session_id=?",
                (task_id, session),
            ).fetchone()
            if row is None:
                raise absent()
            status = str(row["status"])
            if status in {"completed", "failed", "cancelled"}:
                raise conflict(TASK_FINISHED)
            now = time.time()
            claimed = str(row["claimed_by"] or "")
            lease = row["lease_until"]
            held = lease is not None and float(lease) > now
            if claimed and claimed != agent_id and held:
                raise conflict(TASK_HELD)
            db.execute(
                "UPDATE journal SET status='in_progress', claimed_by=?, lease_until=?, at=? "
                "WHERE id=?",
                (agent_id, now + LEASE_SECONDS, now, task_id),
            )

        await self._sessions.transaction(apply)

    async def complete_task(self, account: str, session: str, task_id: int) -> None:
        await self.finish_task(account, session, task_id, status="completed")

    async def finish_task(self, account: str, session: str, task_id: int, *, status: str) -> None:
        """Finish one owned journal row with the helper's real terminal state."""

        def apply(db: sqlite3.Connection) -> None:
            session_row(db, account, session)
            row = db.execute(
                "SELECT id FROM journal WHERE id=? AND session_id=?",
                (task_id, session),
            ).fetchone()
            if row is None:
                raise absent()
            db.execute(
                "UPDATE journal SET status=?, lease_until=NULL, at=? WHERE id=?",
                (status, time.time(), task_id),
            )

        await self._sessions.transaction(apply)

    async def discard_setup(self, account: str, session: str, agent_id: str, task_id: int) -> None:
        """Remove an unexposed helper when the in-process registry refused its start."""

        def apply(db: sqlite3.Connection) -> None:
            session_row(db, account, session)
            _owned_agent(db, account, agent_id)
            db.execute("DELETE FROM journal WHERE id=? AND session_id=?", (task_id, session))
            db.execute("DELETE FROM agents WHERE id=? AND session_id=?", (agent_id, session))

        await self._sessions.transaction(apply)

    async def tasks(self, account: str, session: str) -> tuple[TaskSnapshot, ...]:
        def read(db: sqlite3.Connection) -> tuple[TaskSnapshot, ...]:
            session_row(db, account, session)
            rows = db.execute(
                "SELECT id, title, status, claimed_by, depends_on FROM journal "
                "WHERE session_id=? ORDER BY id",
                (session,),
            ).fetchall()
            snapshots: list[TaskSnapshot] = []
            for row in rows:
                blocked = tuple(part for part in str(row["depends_on"] or "").split(",") if part)
                snapshots.append(
                    TaskSnapshot(
                        id=str(row["id"]),
                        title=str(row["title"]),
                        status=str(row["status"]),
                        claimed_by=str(row["claimed_by"] or ""),
                        blocked_by=blocked,
                    )
                )
            return tuple(snapshots)

        return await self._sessions.worker.call(read)


def _owned_agent(db: sqlite3.Connection, account: str, agent_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = db.execute(
        "SELECT agents.* FROM agents JOIN sessions ON sessions.id=agents.session_id "
        "WHERE agents.id=? AND sessions.account_id=?",
        (agent_id, account),
    ).fetchone()
    if row is None:
        raise absent()
    return row


__all__ = [
    "LEASE_SECONDS",
    "MAIL_BURST",
    "MAIL_MAX_CHARS",
    "MAIL_MAX_HOPS",
    "AgentStore",
]
