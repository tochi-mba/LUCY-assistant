"""Checking a condition on an interval until it holds: the work behind a watch.

"Tell me when CI is green", "when the export lands", "when that helper finishes" are one
shape: look, and if it is not there yet, look again in a little while. The model must not
be the thing that looks again -- a loop of `workspace.run` calls burns a turn's budget on
nothing and holds the conversation open for the whole wait -- so the looking is work,
registered like any other, and the model gets a handle and carries on.

Three things the loop is careful about:

**A failed check is not a failed watch.** The workspace being unreachable for one check is
a fact for the live block, and the watch carries on. Five failed checks in a row is a broken
probe, and the watch ends `failed` with a sentence that says so, because a watch that would
never fire should not sit there for an hour looking like it might.

**What comes back is that it fired, plus a bounded excerpt.** A watch on a build log does not
hand the build log to the model. The excerpt is the lines around the match, or the tail, and
it is cut at a fixed size with the cut made visible.

**Expiry is the registry's timeout.** A watch that never fires ends `timed_out`, and the
notice reads "expired without firing; start it again if you still need it" -- one notice,
then nothing, exactly as a person who asked to be told would expect.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lucy_api.work.types import WorkError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

MAX_EXCERPT = 1_500
"""How much of the evidence a fired watch hands back. Enough to see why; never the file."""

MAX_PATTERN = 200
"""How long a pattern may be. A regular expression longer than this is a program."""

MAX_CONSECUTIVE_FAILURES = 5
"""How many checks may fail in a row before the watch is declared broken."""

MIN_EVERY_SECONDS = 5.0
DEFAULT_EVERY_SECONDS = 15.0
MAX_EVERY_SECONDS = 300.0
"""How often a watch looks. The floor stops a watch from being a busy loop against a sibling;
the ceiling stops one from looking so rarely that "fired" is five minutes stale."""

DEFAULT_FOR_SECONDS = 300.0
MAX_FOR_SECONDS = 3600.0
"""How long a watch lives. Five minutes by default, an hour at most: anything longer is a
standing job, and a standing job is a thing a person should have to ask for again."""

CUT = " […]"


@dataclass(frozen=True, slots=True)
class Check:
    """One look at the condition, as the probe reports it.

    `detail` is the one line the live block shows while the watch waits: "exit 1", "404",
    "no file yet". `excerpt` matters only when it fired. `facts` are the small numbers worth
    carrying into the result -- an exit code, a status -- and never the body.
    """

    fired: bool
    detail: str = ""
    excerpt: str = ""
    facts: Mapping[str, object] = field(default_factory=dict)


type Probe = Callable[[], Awaitable[Check]]
type Sleep = Callable[[float], Awaitable[None]]
type Progress = Callable[[str], None]


class ProbeBrokenError(WorkError):
    """The probe failed several checks in a row; the watch cannot be trusted to fire."""


async def watch(
    probe: Probe,
    *,
    every_seconds: float,
    progress: Progress,
    sleep: Sleep = asyncio.sleep,
) -> dict[str, Any]:
    """Look until it fires. The registry's timeout is what ends a watch that never does."""
    checks = 0
    failures = 0
    while True:
        checks += 1
        try:
            check = await probe()
        except Exception as exc:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                message = f"{failures} checks in a row failed ({type(exc).__name__})"
                raise ProbeBrokenError(message) from exc
            check = Check(fired=False, detail=f"check failed ({type(exc).__name__})")
        else:
            failures = 0
        if check.fired:
            return {
                "fired": True,
                "checks": checks,
                "excerpt": clip(check.excerpt),
                **dict(check.facts),
            }
        progress(f"checked {checks}x, {check.detail or 'not yet'}; next in {every_seconds:.0f}s")
        await sleep(every_seconds)


def compile_pattern(pattern: str) -> re.Pattern[str] | None:
    """The pattern as a regular expression, or `None` for no pattern.

    Raises `ValueError` with a sentence the model can act on: the length cap and the syntax
    error are both its mistake to fix, and both are said in words rather than as a traceback.
    """
    if not pattern:
        return None
    if len(pattern) > MAX_PATTERN:
        message = f"the pattern is {len(pattern)} characters; keep it under {MAX_PATTERN}"
        raise ValueError(message)
    try:
        return re.compile(pattern, re.MULTILINE)
    except re.error as exc:
        message = f"the pattern is not a valid regular expression ({exc.msg})"
        raise ValueError(message) from exc


def excerpt_around(text: str, found: re.Match[str] | None) -> str:
    """The evidence: the match with its surroundings, or the tail when there was no match.

    The tail rather than the head, because the end of a log is where the verdict is.
    """
    if found is None:
        return text[-MAX_EXCERPT:]
    half = MAX_EXCERPT // 2
    start = max(0, found.start() - half)
    return text[start : found.end() + half]


def clip(text: str) -> str:
    """Bound the excerpt, and say so when it was cut."""
    if len(text) <= MAX_EXCERPT:
        return text
    return text[: MAX_EXCERPT - len(CUT)] + CUT


def clamp_every(value: object) -> float:
    """The interval, inside its floor and ceiling. Nothing the model says makes a busy loop."""
    seconds = _number(value, DEFAULT_EVERY_SECONDS)
    return min(max(seconds, MIN_EVERY_SECONDS), MAX_EVERY_SECONDS)


def clamp_for(value: object) -> float:
    """The lifetime, inside its ceiling."""
    seconds = _number(value, DEFAULT_FOR_SECONDS)
    return min(max(seconds, MIN_EVERY_SECONDS), MAX_FOR_SECONDS)


def _number(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


__all__ = [
    "DEFAULT_EVERY_SECONDS",
    "DEFAULT_FOR_SECONDS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_EVERY_SECONDS",
    "MAX_EXCERPT",
    "MAX_FOR_SECONDS",
    "MAX_PATTERN",
    "MIN_EVERY_SECONDS",
    "Check",
    "Probe",
    "ProbeBrokenError",
    "Sleep",
    "clamp_every",
    "clamp_for",
    "clip",
    "compile_pattern",
    "excerpt_around",
    "watch",
]
