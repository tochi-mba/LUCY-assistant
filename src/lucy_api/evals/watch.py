"""Stop a turn the moment it goes wrong, not when it finally comes to rest.

Checks read a turn after it rests, and a turn that loops does not rest: it runs until the
harness's timeout. On 2026-09-30 a turn asked to play a song parked for approval on
``music.play`` ten times. Every approval ran the same call with an unresolved
``$find_track`` and failed the same way, and the harness approved each new ask as it had
the first, for the whole timeout, spending model budget on a failure visible in the first
minute.

So the harness watches the transcript on every poll, and a turn is halted the moment any
of these is true:

``step-error``
    A step ended ``error``. An error is something to look at, not something to approve past.
    A turn whose scenario expects a failure the model should recover from names that
    operation in ``allow_errors``.
``error-item``
    The hub wrote an error into the transcript: the turn itself failed.
``failed-again``
    The same operation failed with the same cause twice in one turn. Halts even when the
    operation is in ``allow_errors``: a recovery that repeats the failure is not a recovery.
``asked-again``
    The turn asked for a call it had already been given an answer to in this turn: the same
    operation with the same arguments. Answering it again is how the loop above ran.

A turn the scenario expects to fail (``status = "failed"``) is not halted for failing:
``step-error`` and ``error-item`` are what it is waiting to see. ``failed-again`` and
``asked-again`` still halt it.

A halt cancels the turn, records why, and ends the scenario: the turns after it would be
said into a conversation already known to be broken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.evals.transcript import exchange_for

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from typing import Any

    from lucy_api.evals.scenario import OpMatch
    from lucy_api.evals.transcript import Ask, Exchange, ToolResult

STEP_ERROR = "step-error"
ERROR_ITEM = "error-item"
FAILED_AGAIN = "failed-again"
ASKED_AGAIN = "asked-again"
RULES = (STEP_ERROR, ERROR_ITEM, FAILED_AGAIN, ASKED_AGAIN)

FAILED = "error"
"""The step status that means the operation ran and failed. ``denied`` is a person's answer,
and ``skipped`` is the executor's, so neither is a failure to look at."""

SHOWN = 160
"""Characters of an error shown in a halt or a progress line; the report keeps all of it."""

_STEP_PREFIX = re.compile(r"^Step '[^']*' failed while running '[^']*': ")
"""The executor names the step it ran, and a replayed step gets a new name each time. The
cause is what follows."""


@dataclass(frozen=True, slots=True)
class Halt:
    """Why a turn was stopped, in the words a report shows."""

    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def watch(
    exchange: Exchange, *, allowed: Sequence[OpMatch] = (), expecting_failure: bool = False
) -> Halt | None:
    """The first reason to stop this turn, or ``None`` while nothing has gone wrong."""
    if exchange.errors and not expecting_failure:
        return Halt(ERROR_ITEM, _short(exchange.errors[0]))
    failed = _failed(exchange.results, allowed, expecting_failure=expecting_failure)
    if failed is not None:
        return failed
    return _asked_again(exchange.asks)


def cause(result: ToolResult) -> str:
    """A failed step's error without the step name the executor put in front of it."""
    return _STEP_PREFIX.sub("", result.error).strip() or "no error text"


class Watcher:
    """One turn, watched across polls: it reports what is new and whether to stop.

    ``on_event`` receives one line per step and ask the first time the transcript shows it,
    so a person watching a run sees the turn as it goes rather than when it ends.
    """

    def __init__(
        self,
        turn_id: str,
        *,
        allowed: Sequence[OpMatch] = (),
        expecting_failure: bool = False,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.turn_id = turn_id
        self._allowed = tuple(allowed)
        self._expecting_failure = expecting_failure
        self._on_event = on_event
        self._results = 0
        self._asks = 0

    def look(self, items: list[dict[str, Any]]) -> tuple[Exchange, Halt | None]:
        """Read the turn as it stands, report what is new, and say whether to stop."""
        exchange = exchange_for(items, self.turn_id)
        for result in exchange.results[self._results :]:
            self._emit(describe_result(result))
        for ask in exchange.asks[self._asks :]:
            self._emit(describe_ask(ask))
        self._results = len(exchange.results)
        self._asks = len(exchange.asks)
        return exchange, watch(
            exchange, allowed=self._allowed, expecting_failure=self._expecting_failure
        )

    def _emit(self, line: str) -> None:
        if self._on_event is not None:
            self._on_event(line)


def describe_result(result: ToolResult) -> str:
    """``· notes.search -> ok``, or the failure with its cause."""
    if result.status == FAILED:
        return f"· {result.operation} -> error: {_short(cause(result))}"
    return f"· {result.operation} -> {result.status}"


def describe_ask(ask: Ask) -> str:
    """``? music.play asks to run (uri=...)``: what a person would be asked."""
    shown = f" ({_short(ask.arguments)})" if ask.arguments else ""
    return f"? {ask.operation} asks to run{shown}"


def _failed(
    results: Sequence[ToolResult], allowed: Sequence[OpMatch], *, expecting_failure: bool
) -> Halt | None:
    seen: set[tuple[str, str]] = set()
    for result in results:
        if result.status != FAILED:
            continue
        why = cause(result)
        key = (result.operation, why)
        if key in seen:
            return Halt(
                FAILED_AGAIN, f"{result.operation} failed the same way twice: {_short(why)}"
            )
        seen.add(key)
        if expecting_failure:
            continue
        if not any(pattern.matches(result.operation) for pattern in allowed):
            return Halt(STEP_ERROR, f"{result.operation} failed: {_short(why)}")
    return None


def _asked_again(asks: Sequence[Ask]) -> Halt | None:
    answered: set[str] = set()
    for ask in asks:
        if ask.call in answered:
            shown = f" ({_short(ask.arguments)})" if ask.arguments else ""
            return Halt(
                ASKED_AGAIN,
                f"{ask.operation} was asked for again after it had been answered{shown}",
            )
        if ask.answer:
            answered.add(ask.call)
    return None


def _short(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SHOWN else flat[: SHOWN - 1] + "…"


__all__ = [
    "ASKED_AGAIN",
    "ERROR_ITEM",
    "FAILED_AGAIN",
    "RULES",
    "STEP_ERROR",
    "Halt",
    "Watcher",
    "cause",
    "describe_ask",
    "describe_result",
    "watch",
]
