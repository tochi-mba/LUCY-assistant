"""What a piece of long-running work is, and what may be said about it.

A helper agent, a four-minute download and a shell command that takes two are the same
thing from where the model is sitting: **work that outlives the step which started it.**
Giving them three mechanisms would mean three places to get cancellation wrong and three
ways for the model to learn that something finished, so they share one shape and differ
only in what produced the result.

Nothing here executes anything. These are the nouns; `registry.py` is the verb.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

MAX_OBJECTIVE = 160
"""How long the sentence describing a piece of work may be.

It is read in a live-state line, a progress line, an approval prompt and a log. A paragraph
in any of those is a paragraph nobody reads, and the sentence exists precisely so that
somebody can read it at a glance.
"""

MAX_PROGRESS = 120
"""How long a progress note may be. Same reasoning, less room: it is the tail of a line."""

MAX_ROLE = 40
"""How long the name of whoever is doing the work may be.

Short on purpose. It is read as the first field of a line and it is a name -- "reviewer",
"download" -- rather than a description, which is what `objective` is for.
"""

MAX_GROUP = 40
"""How long the name of a group of work may be. A name, like `role`: "reviewers"."""

DEFAULT_DEPTH = 1
"""How deep a piece of work started by the main thread is.

Depth exists because a helper may start helpers, and three levels of that is a system
nobody can reason about. It is carried here so that every cap has one place to read it.
"""


class Kind(StrEnum):
    """What produced the result, which is the only way these differ.

    A helper summarises what it found and is held to a token ceiling on the way back. A job
    returns what it produced, which may be enormous and therefore stays behind a reference
    until somebody asks for it. A command returns output, which is the same problem with a
    different name. Everything else -- the handle, the notice, the fetch, the timeout, the
    cancel, the line in the live block -- is shared.
    """

    helper = "helper"
    job = "job"
    command = "command"
    watch = "watch"
    """A condition checked on an interval until it holds. Its result is *that* it fired,
    with a bounded excerpt of the evidence, and its expiry is a different fact from failure:
    "nothing happened in five minutes" is an answer, and the notice says so."""
    subscription = "subscription"
    """A condition a *sibling* checks, which signals Lucy when it holds. The same handle,
    notice and wake as a watch; what differs is that Lucy does not look -- the service that
    can see the condition does -- and the row is durable, so a restart does not lose it."""


class State(StrEnum):
    """Where a piece of work has got to.

    `timed_out` is separate from `failed` on purpose. They are different facts and a person
    told "it failed" when the truth is "it is still going, we stopped waiting" has been
    told something false. It is also the difference between retrying and not.

    `cancelled` is likewise never inferred: a client disconnecting is not a cancellation,
    and the only thing that produces this state is somebody explicitly asking for it.

    `queued` is work that was accepted and has not started, because as many of its kind as
    the person allows are already running. It is in flight -- it has a handle, it shows in
    the live block, it can be cancelled -- and nothing of it has run yet.
    """

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"

    @property
    def finished(self) -> bool:
        return self not in {State.running, State.queued}


@dataclass(frozen=True, slots=True)
class Brief:
    """Everything about a piece of work that is not the work itself.

    It is a struct rather than a handful of keyword arguments because it is written once, by
    whoever decided to start the work, and then read in six places that have nothing to do
    with each other: the live-state line, the completion notice, the audit row, the log, the
    cap that refused it, and the person being asked to approve it. A brief that exists as a
    value can be passed to all six; one that exists as an argument list cannot.

    `objective` is the plain sentence saying what this work is *for*, never a restatement of
    its arguments. "Find out when the tour reaches Europe", not `research.search(query=...)`.
    Nobody can answer "do you approve this?" about an argument list.

    `wake` asks for the session to be woken when this finishes: if no turn is running, one
    is opened with a harness notice so the model can act on the ending rather than wait for
    the person to ask. It needs `account_id`, because opening a turn is done on somebody's
    behalf; a brief that asks to wake without saying whose session it is cannot be honoured
    and is not.

    `group` names a team this work belongs to -- "reviewers" -- so the model can start
    several pieces of work as one unit and be told once when the last of them ends. A member
    of a group does not wake the session on its own ending; the group's ending does.
    """

    session_id: str
    kind: Kind
    role: str
    objective: str
    depth: int = DEFAULT_DEPTH
    timeout_seconds: float = 600.0
    tags: Mapping[str, str] = field(default_factory=dict)
    account_id: str = ""
    wake: bool = False
    group: str = ""


@dataclass(frozen=True, slots=True)
class Handle:
    """What starting a piece of work returns, immediately.

    The step completes; the work does not. This is stable, it survives the end of the turn,
    and it is what everything else addresses -- the notice, the fetch, the cancel and the
    line in the live block all name this id.
    """

    id: str
    kind: Kind
    role: str
    objective: str
    started_at: datetime


@dataclass(frozen=True, slots=True)
class Notice:
    """What the model is told when something finishes.

    It says a thing finished, how it went, how long it took and **roughly how big the answer
    is**. It never carries the answer. Reading a result is a separate, explicit act, so a
    job that produced forty megabytes of log does not arrive uninvited in the context.

    `detail` is one sentence: the timeout that fired, the error's type, or what the helper
    reported in a line. Never a traceback, which is where argument values hide.
    """

    id: str
    kind: Kind
    role: str
    objective: str
    state: State
    elapsed_seconds: float
    tokens: int = 0
    detail: str = ""
    group: str = ""

    def line(self) -> str:
        """One line, for the tool-boundary notice the model actually reads."""
        kind = f"{self.kind}, {self.group}" if self.group else str(self.kind)
        who = f"{self.role} ({kind})"
        parts = [who, self.objective, self.state.value]
        if self.state is State.succeeded and self.tokens:
            parts.append(f"about {self.tokens:,} tokens of result, fetch it to read it")
        if self.detail:
            parts.append(self.detail)
        return " - ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class Result:
    """A finished piece of work's answer, fetched on purpose.

    `payload` is whatever the work produced, unmodified. It is the caller's job to frame and
    scrub it before a model reads it, because this type has no idea whether it is holding a
    helper's report or the contents of a web page.
    """

    id: str
    state: State
    payload: object = None
    tokens: int = 0
    detail: str = ""


@dataclass(slots=True)
class Record:
    """The durable row behind one handle.

    Mutable, because a piece of work changes: it reports progress, it finishes, its notice
    gets delivered, its completion gets shown in a live block. Every one of those is a fact
    about delivery rather than about the work, and keeping them here is what stops the same
    completion being announced twice.
    """

    id: str
    kind: Kind
    role: str
    objective: str
    session_id: str
    started_at: datetime
    depth: int = 1
    timeout_seconds: float = 0.0
    state: State = State.running
    progress: str = ""
    finished_at: datetime | None = None
    payload: object = None
    tokens: int = 0
    detail: str = ""
    noticed: bool = False
    shown: bool = False
    fetched: bool = False
    cancel_requested: bool = False
    tags: dict[str, str] = field(default_factory=dict)
    account_id: str = ""
    wake: bool = False
    group: str = ""
    teamed: bool = False
    """Whether this record's group ending has been told. A group ends once per team."""

    def elapsed(self, now: datetime) -> float:
        """How long it has run, or -- while it is queued -- how long it has waited."""
        end = self.finished_at or now
        return max(0.0, (end - self.started_at).total_seconds())

    def notice(self, now: datetime) -> Notice:
        """This record, as the model is told about it. Never the payload."""
        return Notice(
            id=self.id,
            kind=self.kind,
            role=self.role,
            objective=self.objective,
            state=self.state,
            elapsed_seconds=self.elapsed(now),
            tokens=self.tokens,
            detail=self.detail,
            group=self.group,
        )


@dataclass(frozen=True, slots=True)
class Team:
    """A group of work whose last member has just ended: one notice for all of them.

    Five reviewers started as one team are one question for the model -- "are the reviews
    in?" -- and five separate wakes would open five turns to answer it one fifth at a time.
    So a member of a group wakes nothing on its own, and its group's ending is told once,
    naming every member and how each ended. Reading the results is still one fetch per
    member, by id: this says they ended, never what they found.
    """

    session_id: str
    group: str
    members: tuple[Record, ...]

    @property
    def account_id(self) -> str:
        return next((member.account_id for member in self.members if member.account_id), "")

    @property
    def wake(self) -> bool:
        """Whether any member asked to be woken for; the group wakes in their place."""
        return any(member.wake for member in self.members)

    @property
    def fetched(self) -> bool:
        """Whether every member's result has already been read, so there is no news left."""
        return all(member.fetched for member in self.members)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(member.id for member in self.members)

    def line(self) -> str:
        """One line naming the group and each member's ending, never a result."""
        counted = f"{len(self.members)} member" + ("" if len(self.members) == 1 else "s")
        each = "; ".join(_member_line(member) for member in self.members)
        return f"group {self.group} ({counted}) has ended: {each}"


def _member_line(member: Record) -> str:
    parts = [f"{member.role} {member.id}", member.state.value]
    if member.state is State.succeeded and member.tokens:
        parts.append(f"about {member.tokens:,} tokens of result")
    if member.detail and member.state is not State.succeeded:
        parts.append(member.detail)
    return " - ".join(parts)


class WorkError(Exception):
    """An ending whose message was written for the model, and is safe to show it.

    Every other exception a piece of work raises is recorded by its type name only, because
    a third-party error routinely carries the response body that caused it. Work that knows
    how it ended -- a probe that failed five checks in a row -- says so through this, and the
    sentence reaches the notice.

    `payload` is what the work has to show for itself anyway, readable through `work.result`
    like a success's: a helper that stopped partway still wrote a report of how far it got.
    """

    def __init__(self, message: str, *, payload: object = None) -> None:
        super().__init__(message)
        self.payload = payload


__all__ = [
    "DEFAULT_DEPTH",
    "MAX_GROUP",
    "MAX_OBJECTIVE",
    "MAX_PROGRESS",
    "MAX_ROLE",
    "Brief",
    "Handle",
    "Kind",
    "Notice",
    "Record",
    "Result",
    "State",
    "Team",
    "WorkError",
]
