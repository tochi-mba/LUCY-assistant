"""What a run found, as records a report can be written from and a later run compared to.

Records hold evidence, not verdicts alone. A failed check carries what was actually seen --
the status the turn ended in, the operations that ran, the words that matched -- because a
report that says only "failed" sends somebody back to reproduce the conversation, which is
the expensive part.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lucy_api.evals.transcript import Ask, ToolResult

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
ERROR = "error"
OUTCOMES = (PASSED, FAILED, SKIPPED, ERROR)
"""``skipped``: a capability it requires is not ready, so it never started. ``error``: the
harness could not hold the conversation -- the hub refused a request, or a seed step failed
-- so nothing is known about the model either way."""


@dataclass(frozen=True, slots=True)
class Check:
    """One expectation, whether it held, and what was seen instead when it did not."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    """One operation the harness ran itself, and how it ended.

    ``status`` is the step's own (``ok``, ``error``, ``skipped``), or ``unavailable`` when
    the session cannot call the operation at all, or ``refused`` when the hub turned the
    request down before running it.
    """

    op: str
    input: dict[str, Any]
    status: str
    output: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One turn: what was said, how it came to rest, what it cost, and every check on it."""

    index: int
    said: str
    approve: str
    turn_id: str
    status: str
    termination: str
    seconds: float
    timed_out: bool
    iterations: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None
    reply: str
    results: tuple[ToolResult, ...]
    asks: tuple[Ask, ...]
    errors: tuple[str, ...]
    verify: tuple[InvocationRecord, ...]
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


@dataclass(frozen=True, slots=True)
class ScenarioRecord:
    """One scenario, held once, with one model."""

    scenario: str
    suite: str
    name: str
    summary: str
    path: str
    digest: str
    model: str
    repeat: int
    outcome: str
    reason: str = ""
    session_id: str = ""
    seconds: float = 0.0
    seed: tuple[InvocationRecord, ...] = ()
    turns: tuple[TurnRecord, ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def checks(self) -> tuple[Check, ...]:
        return tuple(check for turn in self.turns for check in turn.checks)

    def to_dict(self) -> dict[str, Any]:
        """The JSON form, with the tallies a reader would otherwise have to count."""
        body = asdict(self)
        checks = self.checks
        body["checks_passed"] = sum(1 for check in checks if check.passed)
        body["checks_total"] = len(checks)
        return body


def outcome_for(turns: tuple[TurnRecord, ...]) -> str:
    """Passed only when every check on every turn held."""
    return PASSED if all(turn.passed for turn in turns) else FAILED


__all__ = [
    "ERROR",
    "FAILED",
    "OUTCOMES",
    "PASSED",
    "SKIPPED",
    "Check",
    "InvocationRecord",
    "ScenarioRecord",
    "TurnRecord",
    "outcome_for",
]
