"""When a turn stops, and why it stopped.

A loop that only knows how to keep going is a loop that occasionally does not. Every reason
to stop is named here, in one place, because the difference between them is what a person
is shown and what a client is allowed to offer next.

The distinction that matters most: **why the model stopped is not why the turn stopped.**
A model can end twenty calls in a row with "I have finished speaking" while the turn ends
because it ran out of iterations. Conflating them produces a client that offers to resume
something unresumable, or refuses to resume something that would have worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Termination(StrEnum):
    """Why Lucy stopped. Distinct from the model's own stop reason."""

    success = "success"
    """It finished. The ordinary case."""

    max_iterations = "error_max_iterations"
    """It kept going round. **Resumable**: the work is real, there was just more of it."""

    max_budget = "error_max_budget"
    """It ran out of the tokens or money it was allowed. Resumable once raised."""

    cancelled = "cancelled"
    """Somebody stopped it on purpose. Not an error and never reported as one."""

    refused = "error_refused"
    """The model declined. **Not resumable**, and a client must not offer to retry it --
    offering a retry on a refusal teaches people that refusals are a rate limit."""

    failed = "error_during_execution"
    """Something broke. Resumable only once whatever broke is fixed."""

    input_required = "input_required"
    """It is waiting on a person: an approval, or an answer to a question."""

    auth_required = "auth_required"
    """It is waiting on a credential. Deliberately not `input_required`: one needs a
    decision and the other needs a connection, and they route to different places."""


RESUMABLE = frozenset({Termination.max_iterations, Termination.max_budget, Termination.failed})
"""The terminations where "try again" is a sensible thing to offer somebody."""

WAITING = frozenset({Termination.input_required, Termination.auth_required})
"""Not finished and not failed. The turn is paused and something outside it must move."""


@dataclass(frozen=True, slots=True)
class Budget:
    """What one turn is allowed to spend before it has to stop and say so."""

    max_iterations: int = 12
    max_tokens: int = 0
    max_seconds: float = 0.0
    max_tool_calls: int = 60

    @property
    def unlimited_tokens(self) -> bool:
        return self.max_tokens <= 0


@dataclass(frozen=True, slots=True)
class Spent:
    """What it has actually used so far."""

    iterations: int = 0
    tokens: int = 0
    seconds: float = 0.0
    tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether to go round again, and what to say if not."""

    stop: bool
    termination: Termination = Termination.success
    detail: str = ""

    @property
    def resumable(self) -> bool:
        return self.termination in RESUMABLE


CONTINUE = Verdict(stop=False)


def should_stop(budget: Budget, spent: Spent) -> Verdict:
    """Check every limit, and name the one that was hit.

    Checked in the order a person would care about: time first, because a turn that has
    been running for two minutes is a turn somebody is staring at; then money; then the
    iteration count, which is the backstop rather than the thing anybody is watching.

    Each limit produces a message that says what was reached and what it was, because
    "budget exceeded" tells nobody anything and "stopped after 12 rounds of tool calls" is
    something a person can act on.
    """
    if budget.max_seconds > 0 and spent.seconds >= budget.max_seconds:
        return Verdict(
            stop=True,
            termination=Termination.max_budget,
            detail=f"stopped after {spent.seconds:.0f}s, the limit for one turn",
        )
    if not budget.unlimited_tokens and spent.tokens >= budget.max_tokens:
        return Verdict(
            stop=True,
            termination=Termination.max_budget,
            detail=f"stopped after {spent.tokens:,} tokens, the limit for one turn",
        )
    if spent.tool_calls >= budget.max_tool_calls:
        return Verdict(
            stop=True,
            termination=Termination.max_iterations,
            detail=f"stopped after {spent.tool_calls} tool calls",
        )
    if spent.iterations >= budget.max_iterations:
        return Verdict(
            stop=True,
            termination=Termination.max_iterations,
            detail=f"stopped after {spent.iterations} rounds of tool calls",
        )
    return CONTINUE


def warning_for(budget: Budget, spent: Spent, *, at: float = 0.8) -> str:
    """A sentence for the model when it is close to a limit, or nothing when it is not.

    Told *before* it runs out, because a model that knows it has one round left will use it
    to write down where it got to. A model that discovers the limit by hitting it writes
    nothing, and the work is lost.
    """
    if budget.max_iterations > 0 and spent.iterations >= budget.max_iterations * at:
        left = budget.max_iterations - spent.iterations
        return (
            f"{left} of {budget.max_iterations} rounds left; finish or write down where you got to"
        )
    if not budget.unlimited_tokens and spent.tokens >= budget.max_tokens * at:
        left = budget.max_tokens - spent.tokens
        return f"{left:,} tokens left in this turn's budget"
    return ""


__all__ = [
    "CONTINUE",
    "RESUMABLE",
    "WAITING",
    "Budget",
    "Spent",
    "Termination",
    "Verdict",
    "should_stop",
    "warning_for",
]
