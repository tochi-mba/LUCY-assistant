"""The workspace group of the live block: where the work is and, on a resume, what is in it.

Every assemble says where the workspace is, whether it can be used, what is running in it and
how long it has left. The first assemble of a turn that is coming back also reads the session's
own files -- the journal's latest entries, the tasks, the recent commits -- because a model that
continues a session without them silently redoes or undoes work. The live block is the right
place for it -- data, not instructions -- and those reads run once a turn, never every round.

Each thing is said once. What runs in the environment -- shells, isolation, branch -- is the
workspace feed's, where each line is a switch the person can turn off, and the live block
renders it inside this group. Before, the path arrived three times over two `workspace`
groups, the first commit as the "last checkpoint" and again in the log, and the journal's
seeded header and `{"tasks":[]}` as if they were news.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lucy_api.clients.environments import ARCHIVED, read_from
from lucy_api.clients.errors import DownstreamError
from lucy_api.context.types import WorkspaceSnapshot
from lucy_api.sessions.scope import PROGRESS_FILE, PROGRESS_STARTER, TASKS_FILE

if TYPE_CHECKING:
    from lucy_api.clients.environments import Environment, EnvironmentsClient, FileText, Ran
    from lucy_api.sessions.scope import WorkspaceScope

GIT_LOG = "git log -3 --oneline"
GIT_STATUS = "git status --short"
JOURNAL_CHARS = 200
"""The end of the journal that is shown: its latest entries, which are what a resume needs."""

JOURNAL_BYTES = 1_024
"""How much of the journal's end is read to find those entries."""

TASKS_BYTES = 16_384
"""The most of `tasks.json` read to summarise it. A partial JSON document is no document."""

TASK_CHARS = 200
COMMIT_CHARS = 60
STATUS_XY = 4
STATUS_SPACE = 2
"""`git status --short` is two status columns, a space, then the path."""


class WorkspaceLive:
    """The workspace group's source, with a resume pass the turn arms on its first assemble."""

    def __init__(
        self,
        client: EnvironmentsClient,
        workspace: WorkspaceScope,
        *,
        profile: str = "",
        retention_hours: int = 24,
    ) -> None:
        self._client = client
        self._workspace = workspace
        self._profile = profile
        self._resume = False
        self._retention_hours = retention_hours

    def arm(self, resume: bool) -> None:
        """Spend the expensive reads only when this assemble is a resume."""
        self._resume = resume

    async def fetch(self, session_id: str) -> WorkspaceSnapshot | None:
        del session_id
        current = await self._environment()
        if current is None:
            return WorkspaceSnapshot(path=self._workspace.root, ready=False)
        usable = current.state != ARCHIVED
        snapshot = (
            await orient(self._client, self._workspace)
            if usable and self._resume
            else WorkspaceSnapshot(path=self._workspace.root, ready=usable)
        )
        return replace(snapshot, expires_in_seconds=self._expires(current))

    async def _environment(self) -> Environment | None:
        """This session's workspace as the sandbox sees it, or None when it cannot say.

        None is not ready: a workspace the sandbox does not list, or cannot be asked about,
        is not one a step could use.
        """
        try:
            environments = await self._client.environments(profile=self._profile)
        except DownstreamError:
            return None
        return next(
            (
                item
                for item in environments
                if item.environment_id == self._workspace.environment_id
            ),
            None,
        )

    def _expires(self, current: Environment) -> float | None:
        """Seconds until the sandbox archives this workspace, as the sandbox reckons it.

        Its own reaper decides, from the environment's stamped time to live and its last
        activity, and never while a shell is open (Environments-api `reap`). The hub's
        `workspace_retention_hours` stands in only for an environment stamped with no time
        to live. Reading the hub's setting first showed "sandbox expires in 22h" about a
        workspace already wiped, and an archived one is expired whatever the clock says.
        """
        if current.state == ARCHIVED:
            return 0.0
        if current.shells_running or current.last_activity_at is None:
            return None
        activity = current.last_activity_at
        if activity.tzinfo is None:
            activity = activity.replace(tzinfo=UTC)
        ttl = current.idle_ttl_seconds
        if ttl is None:
            ttl = self._retention_hours * 3600.0
        return max(0.0, ttl - (datetime.now(UTC) - activity).total_seconds())


async def orient(client: EnvironmentsClient, workspace: WorkspaceScope) -> WorkspaceSnapshot:
    """Read the session's own files. A git outage still returns the journal and the tasks."""
    env_id = workspace.environment_id
    root = workspace.root
    journal = await _journal(client, env_id, posixpath.join(root, PROGRESS_FILE))
    tasks = await _tasks(client, env_id, posixpath.join(root, TASKS_FILE))
    log = await _run(client, env_id, GIT_LOG, root)
    status = await _run(client, env_id, GIT_STATUS, root)
    return WorkspaceSnapshot(
        path=root,
        ready=True,
        changed_files=_status_paths(status or ""),
        commits=tuple(_clip(line, COMMIT_CHARS) for line in (log or "").splitlines() if line),
        journal=journal,
        tasks=tasks,
        git_missing=log is None or status is None,
    )


async def _journal(client: EnvironmentsClient, env_id: str, path: str) -> str:
    """The journal's latest entries, or nothing when it holds only what it was seeded with.

    The end, not the start: the journal is append-only, so its first lines are the oldest
    and the seeded header. Reading the head showed a resuming model "# Progress Append-only
    journal for this conversation" and, once the file grew, the entries least worth knowing.
    """
    try:
        head = await client.read(env_id, path, max_bytes=JOURNAL_BYTES)
        window = head
        if head.truncated:
            offset = max(0, head.size - JOURNAL_BYTES)
            window = await read_from(client, env_id, path, offset, max_bytes=JOURNAL_BYTES)
    except (DownstreamError, KeyError):
        return ""
    body = _latest(window)
    compact = " ".join(body.split())
    if len(compact) <= JOURNAL_CHARS:
        return compact
    return "…" + compact[-JOURNAL_CHARS:].lstrip()


def _latest(window: FileText) -> str:
    """What the window says past the seeded header, whole lines only."""
    if window.binary:
        return ""
    if window.offset == 0:
        return window.content.removeprefix(PROGRESS_STARTER)
    # A window that starts part-way through the file starts part-way through a line.
    _partial, _newline, rest = window.content.partition("\n")
    return rest


async def _tasks(client: EnvironmentsClient, env_id: str, path: str) -> str:
    """`tasks.json` as a line a person would write: how many, and what they are.

    Nothing when there are none, because an empty list is not news. A file too large to
    read whole, or that is not JSON, is said to be so rather than shown as a fragment.
    """
    try:
        read = await client.read(env_id, path, max_bytes=TASKS_BYTES)
    except (DownstreamError, KeyError):
        return ""
    if read.truncated or read.binary:
        return f"{read.size:,} bytes, too large to summarise here"
    try:
        parsed = json.loads(read.content)
    except ValueError:
        return "not valid JSON"
    listed = parsed.get("tasks") if isinstance(parsed, dict) else parsed
    if not isinstance(listed, list):
        return "not a list of tasks"
    if not listed:
        return ""
    count = f"{len(listed)} task" + ("" if len(listed) == 1 else "s")
    return _clip(f"{count}: " + "; ".join(_task_name(task) for task in listed), TASK_CHARS)


def _task_name(task: Any) -> str:
    if not isinstance(task, dict):
        return str(task)
    title = str(task.get("title") or task.get("name") or task.get("id") or "untitled")
    status = str(task.get("status") or "")
    return f"{title} ({status})" if status else title


async def _run(client: EnvironmentsClient, env_id: str, command: str, cwd: str) -> str | None:
    try:
        ran: Ran = await client.run(env_id, command, cwd=cwd)
    except DownstreamError:
        return None
    if ran.exit_code not in {0, None}:
        return None
    return ran.output


def _status_paths(output: str) -> tuple[str, ...]:
    names: list[str] = []
    for line in output.splitlines():
        if len(line) < STATUS_XY or line[STATUS_SPACE] != " ":
            continue
        path = line[STATUS_SPACE + 1 :].strip()
        if path:
            names.append(path.replace("\\", "/"))
    return tuple(names)


def _clip(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


__all__ = ["GIT_LOG", "GIT_STATUS", "WorkspaceLive", "orient"]
