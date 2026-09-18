"""The resume ritual: cwd, journal, git log, tasks, then a cheap smoke check.

A model that continues a session without this silently redoes or undoes work. The live
block is the right place for it — data, not instructions — and it runs only on the first
assemble of a turn that is coming back, never on every tool round.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

from lucy_api.clients.errors import DownstreamError
from lucy_api.context.types import WorkspaceSnapshot
from lucy_api.sessions.scope import PROGRESS_FILE, TASKS_FILE

if TYPE_CHECKING:
    from lucy_api.clients.environments import EnvironmentsClient, Ran
    from lucy_api.sessions.scope import WorkspaceScope

GIT_LOG = "git log -5 --oneline"
GIT_STATUS = "git status --short"
JOURNAL_CHARS = 400
TASK_CHARS = 240
LOG_CHARS = 400
SMOKE_OK = "git is available"
SMOKE_MISSING = "git is not available in this sandbox"
STATUS_XY = 4
STATUS_SPACE = 2
"""`git status --short` is two status columns, a space, then the path."""


class WorkspaceLive:
    """The workspace group of the live block, with an optional resume pass."""

    def __init__(self, client: EnvironmentsClient, workspace: WorkspaceScope) -> None:
        self._client = client
        self._workspace = workspace
        self._resume = False

    def arm(self, resume: bool) -> None:
        """Spend the expensive reads only when this assemble is a resume."""
        self._resume = resume

    async def fetch(self, session_id: str) -> WorkspaceSnapshot | None:
        del session_id
        if not self._resume:
            return WorkspaceSnapshot(path=self._workspace.root, ready=True)
        return await orient(self._client, self._workspace)


async def orient(client: EnvironmentsClient, workspace: WorkspaceScope) -> WorkspaceSnapshot:
    """Read the session's own files. A git outage still returns cwd and the journal."""
    env_id = workspace.environment_id
    root = workspace.root
    journal = await _read(client, env_id, posixpath.join(root, PROGRESS_FILE), JOURNAL_CHARS)
    tasks = await _read(client, env_id, posixpath.join(root, TASKS_FILE), TASK_CHARS)
    log = await _run(client, env_id, GIT_LOG, root)
    status = await _run(client, env_id, GIT_STATUS, root)
    smoke = SMOKE_OK if log is not None and status is not None else SMOKE_MISSING
    checkpoint = ""
    if log:
        checkpoint = log.splitlines()[0][:LOG_CHARS]
    return WorkspaceSnapshot(
        path=root,
        ready=True,
        changed_files=_status_paths(status or ""),
        last_checkpoint=checkpoint,
        cwd=root,
        journal=journal,
        git_log=_clip(log or "", LOG_CHARS),
        tasks=tasks,
        smoke=smoke,
    )


async def _read(client: EnvironmentsClient, env_id: str, path: str, limit: int) -> str:
    try:
        body = await client.read(env_id, path, max_bytes=limit * 2)
    except (DownstreamError, KeyError):
        return ""
    return _clip(body.content, limit)


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
    omitted = len(compact) - limit
    return f"{compact[:limit]}… [{omitted} characters omitted]"


__all__ = ["GIT_LOG", "GIT_STATUS", "WorkspaceLive", "orient"]
