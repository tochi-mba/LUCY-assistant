"""Everything that is true of one session, resolved once and passed down.

Ask "which profile is this?" in enough places and eventually two of them disagree. The
answer gets re-derived from a token here, a path segment there, a default somewhere else,
and the day it goes wrong is the day a background job writes a work memory into a home
profile. So it is derived **once**, at the top of a turn, into this object, and everything
downstream -- every capability, every client call, every child agent, every event -- reads
it rather than working it out again.

That is worth stating as a rule: **nothing below this module may take `account_id` or
`profile` from anywhere else.** Not from a request body, not from a path parameter, not
from a header. The account comes from the verified token and the profile comes from the
session row, and this is the one place those two meet.

## It is also the confinement boundary

Environments-api keys a workspace on the *account*. Two sessions belonging to one person
therefore land in the same sandbox, and without something in between, one conversation can
read and delete another's files. Nothing downstream will catch that, because from the
sandbox's point of view it is all the same person doing all of it.

So Lucy confines its own sessions, and `WorkspaceScope` is where that happens. One function
computes a session's subtree, one function decides whether a path is inside it, and every
workspace operation goes through the second one. Keeping it to two functions is the point:
a containment check scattered across nine call sites is a containment check with a hole in
it.

## Why a child agent gets a narrowed copy rather than the same object

An agent inherits its parent's account, profile and session -- it is working on the same
person's behalf, in the same conversation -- but gets its own subtree and may only narrow
its permission mode, never widen it. `for_agent` is the only way to make one, so "a child
escalated" is not a thing that can be expressed.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

SESSIONS_ROOT = "sessions"
AGENTS_DIR = "agents"
SCRIPTS_DIR = "scripts"

PROGRESS_FILE = "progress.md"
TASKS_FILE = "tasks.json"
"""The two files every session workspace is bootstrapped with.

`tasks.json` is structured rather than Markdown on purpose: a model is measurably less
willing to overwrite JSON wholesale, and the operations it gets are append and mark-status
rather than write-the-whole-file.
"""

PROGRESS_STARTER = "# Progress\n\nAppend-only journal for this conversation.\n"
TASKS_STARTER = '{"tasks":[]}\n'
GIT_INIT = "git init"
GIT_BASELINE = (
    "git add -A && git -c user.email=lucy@local -c user.name=lucy "
    "commit --allow-empty -m session-start"
)

MODE_WIDTH = ("plan", "ask", "accept_edits", "auto")
"""Permission modes from narrowest to widest. A child may move left, never right."""


def disabled_in(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The capabilities one conversation turned off, as its stored row says.

    Read here, beside the rest of what a row says about a session, so the turn that starts
    from the row and the request that prepares it agree on the list.
    """
    listed = row.get("disabled_capabilities")
    return tuple(str(name) for name in listed) if isinstance(listed, list) else ()


class ConfinementError(Exception):
    """A path that tried to leave the session it belongs to."""


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    """One session's corner of a sandbox, and the only thing that decides what is inside it."""

    environment_id: str
    session_id: str
    agent_id: str = ""
    ready: bool = False
    expires_at: float | None = None

    @property
    def root(self) -> str:
        """The session's subtree, or the agent's subtree beneath it.

        Agents are nested under their session rather than beside it, so that deleting a
        session takes its children's work with it and a listing of the session shows what
        its helpers did.
        """
        base = posixpath.join(SESSIONS_ROOT, self.session_id)
        return posixpath.join(base, AGENTS_DIR, self.agent_id) if self.agent_id else base

    @property
    def scripts(self) -> str:
        """Where a script is written before it is run, so it is reviewable and re-runnable."""
        return posixpath.join(self.root, SCRIPTS_DIR)

    def resolve(self, relative: str) -> str:
        """The absolute path inside the sandbox, or a refusal.

        Normalisation happens before the check, not after, because `a/../../b` is only
        obviously an escape once it has been normalised. Backslashes are folded first: the
        sandbox is POSIX, but a path can arrive from a Windows client, and `..\\` would
        otherwise sail past a check that only knows about `../`.
        """
        if not relative or relative in {".", "/"}:
            return self.root
        folded = relative.replace("\\", "/")
        if folded.startswith("/"):
            message = f"{relative!r} is absolute; workspace paths are relative to the session"
            raise ConfinementError(message)
        candidate = posixpath.normpath(posixpath.join(self.root, folded))
        if candidate != self.root and not candidate.startswith(self.root + "/"):
            message = f"{relative!r} resolves outside this session's workspace"
            raise ConfinementError(message)
        return candidate

    def contains(self, path: str) -> bool:
        """Whether a path is inside this session, without raising. For filtering a listing."""
        try:
            self.resolve(path)
        except ConfinementError:
            return False
        return True


@dataclass(frozen=True, slots=True)
class SessionScope:
    """Who this is, where they are, and everything true only here.

    `facts()` exists because this object is rendered in three places -- the live state
    block the model reads, the HTTP representation a client reads, and a log line -- and
    three renderers that each pick their own fields will drift. One mapping, three readers.
    """

    account_id: str
    profile: str
    session_id: str

    # Where it came from.
    parent_session_id: str | None = None
    forked_from_item: str | None = None
    harness_version: str = ""
    created_at: float = 0.0

    # How it behaves.
    model: str = ""
    persona: str = "default"
    thinking: str = "default"
    input_policy: str = "enqueue"
    permission_mode: str = "ask"
    durability: str = "durable"
    incognito: bool = False

    # Where it stands.
    status: str = "idle"
    title: str = ""
    turn_number: int = 0

    # What it is working in.
    workspace: WorkspaceScope | None = None

    # Which turn is running, when one is. Empty between turns.
    turn_id: str = ""

    # Who is doing the work. Empty on the main thread.
    agent_id: str = ""
    depth: int = 0

    # What it has cost so far.
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micros: int = 0

    extra: Mapping[str, Any] = field(default_factory=dict)
    """Room for a subsystem to attach something scoped to this session without changing
    this class. Rendered alongside the rest, so a new fact does not need a new renderer."""

    @property
    def is_agent(self) -> bool:
        return bool(self.agent_id)

    @property
    def workspace_root(self) -> str:
        """The subtree this scope may touch, or empty when it has no sandbox."""
        return self.workspace.root if self.workspace else ""

    def for_agent(self, agent_id: str, *, permission_mode: str | None = None) -> SessionScope:
        """A child's scope: the same person and conversation, a narrower everything else.

        The mode may only narrow. A child that could widen its own permissions would make
        every approval the parent answered meaningless, and "ask the child to do it" would
        become the way around any refusal.
        """
        mode = self.permission_mode
        if permission_mode is not None:
            widest = MODE_WIDTH.index(mode) if mode in MODE_WIDTH else len(MODE_WIDTH) - 1
            asked = MODE_WIDTH.index(permission_mode) if permission_mode in MODE_WIDTH else widest
            mode = MODE_WIDTH[min(asked, widest)]
        return replace(
            self,
            agent_id=agent_id,
            depth=self.depth + 1,
            permission_mode=mode,
            workspace=(
                replace(self.workspace, agent_id=agent_id) if self.workspace is not None else None
            ),
        )

    def facts(self) -> dict[str, Any]:
        """Everything scoped to this session, in one mapping, for whoever is rendering it."""
        facts: dict[str, Any] = {
            "account_id": self.account_id,
            "profile": self.profile,
            "session_id": self.session_id,
            "title": self.title,
            "status": self.status,
            "turn_number": self.turn_number,
            "model": self.model,
            "persona": self.persona,
            "thinking": self.thinking,
            "input_policy": self.input_policy,
            "permission_mode": self.permission_mode,
            "durability": self.durability,
            "incognito": self.incognito,
            "harness_version": self.harness_version,
            "created_at": self.created_at,
            "parent_session_id": self.parent_session_id,
            "forked_from_item": self.forked_from_item,
            "turn_id": self.turn_id or None,
            "agent_id": self.agent_id or None,
            "depth": self.depth,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_micros": self.cost_micros,
            },
            "workspace": None
            if self.workspace is None
            else {
                "environment_id": self.workspace.environment_id,
                "root": self.workspace.root,
                "scripts": self.workspace.scripts,
                "ready": self.workspace.ready,
                "expires_at": self.workspace.expires_at,
            },
        }
        facts.update(self.extra)
        return facts


def scope_from_row(
    row: Mapping[str, Any],
    *,
    account_id: str,
    turn_number: int = 0,
    workspace_ready: bool = False,
    workspace_expires_at: float | None = None,
) -> SessionScope:
    """Build a scope from a stored session, with the account from the verified token.

    The account is a parameter rather than being read from the row, and that is deliberate:
    the row is storage and the token is authority. Passing it in means a caller cannot
    accidentally build a scope for a session they did not prove they own -- they had to have
    a verified account id in their hand to call this at all.
    """
    environment = row.get("workspace_environment_id") or ""
    session_id = str(row["id"])
    workspace = (
        WorkspaceScope(
            environment_id=str(environment),
            session_id=session_id,
            ready=workspace_ready,
            expires_at=workspace_expires_at,
        )
        if environment
        else None
    )
    return SessionScope(
        account_id=account_id,
        profile=str(row.get("profile", "personal")),
        session_id=session_id,
        parent_session_id=row.get("parent_session_id"),
        forked_from_item=row.get("forked_from_item"),
        harness_version=str(row.get("harness_version", "")),
        created_at=float(row.get("created_at", 0.0)),
        model=str(row.get("model", "")),
        persona=str(row.get("persona", "default")),
        thinking=str(row.get("thinking_config", "default")),
        input_policy=str(row.get("input_policy", "enqueue")),
        permission_mode=str(row.get("permission_mode", "ask")),
        durability=str(row.get("durability_mode", "durable")),
        incognito=bool(row.get("incognito", 0)),
        status=str(row.get("status", "idle")),
        title=str(row.get("title", "")),
        turn_number=turn_number,
        workspace=workspace,
        input_tokens=int(row.get("input_tokens", 0)),
        output_tokens=int(row.get("output_tokens", 0)),
        cost_micros=int(row.get("cost_micros", 0)),
    )


__all__ = [
    "AGENTS_DIR",
    "GIT_BASELINE",
    "GIT_INIT",
    "MODE_WIDTH",
    "PROGRESS_FILE",
    "PROGRESS_STARTER",
    "SCRIPTS_DIR",
    "SESSIONS_ROOT",
    "TASKS_FILE",
    "TASKS_STARTER",
    "ConfinementError",
    "SessionScope",
    "WorkspaceScope",
    "scope_from_row",
]
