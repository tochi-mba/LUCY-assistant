"""Task rows and transcripts: what each delegation is, and everything it said.

A task is durable on purpose. The hub's work registry forgets a poller on restart; the
bridge's rows are what let "check on that refactor from yesterday" work from any session.
One SQLite file holds the rows; each task appends its raw stream-json lines to its own
JSONL transcript, so nothing Claude Code said is lost to a crash, and a person can read
the whole run from disk.

SQLite is used synchronously under one lock and called through ``asyncio.to_thread`` by
the service. The bridge serves one person's machine: contention is two tasks, not two
thousand, and a worker thread would be machinery without a reader.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

RESULT_CHARS = 20_000
"""The most of a turn's final answer a row keeps. The transcript holds the rest."""

SNIPPET_CHARS = 160
"""How much of the last text block the live counters keep, for a one-line progress read."""


class TaskState(StrEnum):
    """Where one delegation is.

    ``idle`` is the state helpers do not have: the turn is over, the Claude Code session is
    still resumable, and a follow-up message starts its next turn. ``failed`` and
    ``cancelled`` are terminal for the *bridge's* loop, but the session may still be
    resumable by hand; ``resumable`` on the row says so.
    """

    queued = "queued"
    running = "running"
    idle = "idle"
    failed = "failed"
    cancelled = "cancelled"


LIVE = frozenset({TaskState.queued, TaskState.running})
TERMINAL = frozenset({TaskState.failed, TaskState.cancelled})


@dataclass
class Task:
    """One delegation: its brief, where it runs, and what has happened so far."""

    id: str
    account_id: str
    session_id: str
    title: str
    brief: str
    directory: str
    run_level: str
    state: TaskState
    detail: str = ""
    result: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    tool_uses: int = 0
    last_tool: str = ""
    last_text: str = ""
    queued_messages: list[str] = field(default_factory=list)
    resumable: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0

    def public(self) -> dict[str, Any]:
        """The row as the hub reads it. Everything here reaches a model eventually."""
        return {
            "id": self.id,
            "title": self.title,
            "brief": self.brief,
            "directory": self.directory,
            "run_level": self.run_level,
            "state": self.state.value,
            "detail": self.detail,
            "result": self.result,
            "turns": self.turns,
            "cost_usd": round(self.cost_usd, 6),
            "tool_uses": self.tool_uses,
            "last_tool": self.last_tool,
            "last_text": self.last_text,
            "queued_messages": len(self.queued_messages),
            "resumable": self.resumable,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT NOT NULL,
    brief TEXT NOT NULL,
    directory TEXT NOT NULL,
    run_level TEXT NOT NULL,
    state TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    turns INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    tool_uses INTEGER NOT NULL DEFAULT 0,
    last_tool TEXT NOT NULL DEFAULT '',
    last_text TEXT NOT NULL DEFAULT '',
    queued_messages TEXT NOT NULL DEFAULT '[]',
    resumable INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

INTERRUPTED = "the bridge restarted while this ran; the session is resumable with a message"


class TaskStore:
    """Rows in SQLite, transcripts as JSONL files beside them."""

    def __init__(self, var_dir: str, *, now: Any = time.time) -> None:
        self._now = now
        self._dir = Path(var_dir)
        self._transcripts = self._dir / "transcripts"
        self._transcripts.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._dir / "tasks.sqlite3"), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ writing

    def create(
        self, *, account_id: str, brief: str, directory: str, run_level: str, title: str
    ) -> Task:
        task = Task(
            id=f"tsk_{uuid.uuid4().hex[:16]}",
            account_id=account_id,
            session_id=str(uuid.uuid4()),
            title=title,
            brief=brief,
            directory=directory,
            run_level=run_level,
            state=TaskState.queued,
            created_at=self._now(),
            updated_at=self._now(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks (id, account_id, session_id, title, brief, directory,"
                " run_level, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    task.id,
                    task.account_id,
                    task.session_id,
                    task.title,
                    task.brief,
                    task.directory,
                    task.run_level,
                    task.state.value,
                    task.created_at,
                    task.updated_at,
                ),
            )
            self._db.commit()
        return task

    def save(self, task: Task) -> None:
        task.updated_at = self._now()
        task.result = task.result[:RESULT_CHARS]
        with self._lock:
            self._db.execute(
                "UPDATE tasks SET state=?, detail=?, result=?, turns=?, cost_usd=?,"
                " tool_uses=?, last_tool=?, last_text=?, queued_messages=?, resumable=?,"
                " updated_at=? WHERE id=?",
                (
                    task.state.value,
                    task.detail,
                    task.result,
                    task.turns,
                    task.cost_usd,
                    task.tool_uses,
                    task.last_tool,
                    task.last_text,
                    json.dumps(task.queued_messages),
                    int(task.resumable),
                    task.updated_at,
                    task.id,
                ),
            )
            self._db.commit()

    def mark_interrupted(self) -> int:
        """Fail what a dead process left `running`, honestly, at startup.

        A row that says running with no process behind it would wait forever. The session
        file Claude Code kept is still there, so the row stays resumable.
        """
        with self._lock:
            changed = self._db.execute(
                "UPDATE tasks SET state=?, detail=?, resumable=1, updated_at=? WHERE state=?",
                (TaskState.failed.value, INTERRUPTED, self._now(), TaskState.running.value),
            ).rowcount
            self._db.commit()
        return changed

    # ------------------------------------------------------------------ reading

    def get(self, account_id: str, task_id: str) -> Task | None:
        """One task, or ``None`` -- a stranger's task reads exactly like a missing one."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE id=? AND account_id=?", (task_id, account_id)
            ).fetchone()
        return _task(row) if row is not None else None

    def get_any(self, task_id: str) -> Task | None:
        """One task with no account filter: for the service settling its own turns only.

        Never reachable from a route; every route goes through :meth:`get`.
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _task(row) if row is not None else None

    def for_account(self, account_id: str) -> list[Task]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE account_id=? ORDER BY created_at", (account_id,)
            ).fetchall()
        return [_task(row) for row in rows]

    def next_queued(self) -> Task | None:
        """The oldest task still waiting for a slot, whoever it belongs to."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE state=? ORDER BY created_at LIMIT 1",
                (TaskState.queued.value,),
            ).fetchone()
        return _task(row) if row is not None else None

    def live_count(self) -> int:
        with self._lock:
            (count,) = self._db.execute(
                "SELECT COUNT(*) FROM tasks WHERE state=?", (TaskState.running.value,)
            ).fetchone()
        return int(count)

    # ------------------------------------------------------------------ transcripts

    def append_transcript(self, task_id: str, line: str) -> None:
        with (self._transcripts / f"{task_id}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def transcript_tail(self, task_id: str, chars: int) -> str:
        path = self._transcripts / f"{task_id}.jsonl"
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        return text[-chars:] if chars < len(text) else text

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        account_id=row["account_id"],
        session_id=row["session_id"],
        title=row["title"],
        brief=row["brief"],
        directory=row["directory"],
        run_level=row["run_level"],
        state=TaskState(row["state"]),
        detail=row["detail"],
        result=row["result"],
        turns=row["turns"],
        cost_usd=row["cost_usd"],
        tool_uses=row["tool_uses"],
        last_tool=row["last_tool"],
        last_text=row["last_text"],
        queued_messages=list(json.loads(row["queued_messages"])),
        resumable=bool(row["resumable"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
