"""What a scenario says, once its file has been read and every value checked.

A scenario is a conversation a person would really have, written down with what must be
true after each turn. The file is TOML (see :mod:`lucy_api.evals.loader`); this module is
the validated shape it becomes, so nothing downstream ever handles a raw table or a regex
that has not compiled.

Everything here is frozen. A scenario is a constant: the value of the list is that the same
words are said to every model, every time, and a runner that could mutate one would be a
harness that quietly tests something different on the second run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import re

YES = "yes"
NO = "no"
YES_SESSION = "yes-session"
IGNORE = "ignore"
APPROVE_VALUES = (YES, NO, YES_SESSION, IGNORE)
"""How the harness answers every approval a turn parks on.

``yes`` approves once, ``yes-session`` approves for the rest of this conversation (the
hub stores that grant against the session, never the profile), ``no`` refuses, and
``ignore`` leaves the ask unanswered so the turn stays parked -- which is how a scenario
tests what Lucy says about work still waiting on the person."""

PERMISSION_MODES = ("ask", "accept_edits", "plan", "auto")
"""The hub's session permission modes, spelled as its API spells them."""

COMPLETED = "completed"
INPUT_REQUIRED = "input_required"
AUTH_REQUIRED = "auth_required"
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
TURN_STATUSES = (*TERMINAL_STATUSES, INPUT_REQUIRED, AUTH_REQUIRED)
"""Where a turn can come to rest. A running or queued turn is still moving."""

OK = "ok"
STEP_STATUSES = (OK, "error", "skipped", "denied")
"""How one executed operation ended, as the transcript records it."""

LIFETIMES = {YES: "once", NO: "once", YES_SESSION: "session"}
"""The approval lifetime each answer is sent with."""


@dataclass(frozen=True, slots=True)
class Pattern:
    """A regular expression and the text it was written as, for evidence."""

    source: str
    regex: re.Pattern[str]

    def search(self, text: str) -> re.Match[str] | None:
        return self.regex.search(text)


@dataclass(frozen=True, slots=True)
class OpMatch:
    """One or more operation patterns, any of which will do.

    ``notes.setFact|notes.remember`` accepts either, and ``workspace.*`` accepts every
    workspace operation. A model is free to choose between equivalent operations; a
    scenario that pinned one would fail a model for a choice that was not wrong.
    """

    source: str
    alternatives: tuple[str, ...]

    def matches(self, operation: str) -> bool:
        return any(fnmatchcase(operation, pattern) for pattern in self.alternatives)


@dataclass(frozen=True, slots=True)
class ResultExpect:
    """What one operation's tool result, as the model was shown it, must and must not say."""

    op: OpMatch
    matches: tuple[Pattern, ...] = ()
    avoids: tuple[Pattern, ...] = ()


@dataclass(frozen=True, slots=True)
class Expect:
    """What must be true when one turn comes to rest."""

    status: str = COMPLETED
    termination: str | None = None
    ran: tuple[OpMatch, ...] = ()
    not_ran: tuple[OpMatch, ...] = ()
    not_attempted: tuple[OpMatch, ...] = ()
    approvals: tuple[OpMatch, ...] = ()
    reply_matches: tuple[Pattern, ...] = ()
    reply_avoids: tuple[Pattern, ...] = ()
    reply_nonempty: bool = True
    no_leaks: bool = True
    max_seconds: float | None = None
    results: tuple[ResultExpect, ...] = ()


@dataclass(frozen=True, slots=True)
class Invocation:
    """One operation the harness runs itself, through the hub's invoke route.

    Used before the first turn (``seed``: plant a file) and after a turn (``verify``: read
    the file back). Verification is how a scenario proves something happened instead of
    trusting the model's word for it -- a weak model has been seen to say "Done. Your
    calculator website is ready" having written nothing at all.
    """

    op: str
    input: dict[str, Any] = field(default_factory=dict)
    status: str = OK
    output_matches: tuple[Pattern, ...] = ()
    output_avoids: tuple[Pattern, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnSpec:
    """One thing the person says, how the harness answers asks, and what must follow."""

    say: str
    approve: str = YES
    timeout_seconds: float | None = None
    expect: Expect = field(default_factory=Expect)
    verify: tuple[Invocation, ...] = ()


@dataclass(frozen=True, slots=True)
class Scenario:
    """One conversation: its own session, its turns in order, and where it came from."""

    name: str
    suite: str
    path: str
    digest: str
    """SHA-256 of the file, so a comparison can say the scenario itself changed."""

    summary: str
    turns: tuple[TurnSpec, ...]
    tags: tuple[str, ...] = ()
    permission_mode: str = "ask"
    incognito: bool = False
    requires: tuple[str, ...] = ()
    seed: tuple[Invocation, ...] = ()

    @property
    def qualified(self) -> str:
        """``suite/name``: unique across every suite a run loads."""
        return f"{self.suite}/{self.name}"


@dataclass(frozen=True, slots=True)
class Suite:
    """A folder of scenarios. ``origin`` is ``shipped`` or the path it was read from."""

    name: str
    origin: str
    scenarios: tuple[Scenario, ...]


__all__ = [
    "APPROVE_VALUES",
    "AUTH_REQUIRED",
    "COMPLETED",
    "IGNORE",
    "INPUT_REQUIRED",
    "LIFETIMES",
    "NO",
    "OK",
    "PERMISSION_MODES",
    "STEP_STATUSES",
    "TERMINAL_STATUSES",
    "TURN_STATUSES",
    "YES",
    "YES_SESSION",
    "Expect",
    "Invocation",
    "OpMatch",
    "Pattern",
    "ResultExpect",
    "Scenario",
    "Suite",
    "TurnSpec",
]
