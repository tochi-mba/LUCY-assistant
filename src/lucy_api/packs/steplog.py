"""One log line per step a plan runs: which operation, how long it took, and how it ended.

A plan is where the work happens, and until this the log said nothing about any of it: the
eleven correlation fields were on every line and `operation`, `duration_ms` and `outcome` were
null on all of them. A person debugging "why was that turn slow" or "which call failed" had the
transcript and nothing else.

weftai calls the hooks here around every step it runs. A step that failed is logged with its
error's *type*, never its message, for the reason `lucy_api.core.logging` gives: a message
routinely carries the value that caused it. A step that never ran -- skipped because what it
needed failed -- has no line, because nothing ran.

A hook must never change what ran. weftai calls `afterStep` inside the step's own `try`, so
a hook that raised would turn a step that succeeded into one that failed. Nothing here can
raise: every line is written under a guard, and an unlogged step is the worst that happens.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger("lucy_api.steps")


def step_hooks() -> dict[str, Any]:
    """The weftai hooks for one plan's run. A fresh set per runtime, so timings never mix."""
    started: dict[str, float] = {}

    def before(args: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            started[str(args["step"].id)] = time.perf_counter()

    def after(args: dict[str, Any]) -> None:
        _ended(args, started, "ok")

    def failed(args: dict[str, Any]) -> None:
        _ended(args, started, "error")

    return {"beforeStep": before, "afterStep": after, "onStepError": failed}


def _ended(args: dict[str, Any], started: dict[str, float], outcome: str) -> None:
    """One line about a step that ended, or none at all if the hook's arguments are odd."""
    with contextlib.suppress(Exception):
        step = args["step"]
        began = started.pop(str(step.id), None)
        operation = str(step.operation.name)
        extra: dict[str, Any] = {
            "capability": operation.split(".", 1)[0],
            "operation": operation,
            "duration_ms": None
            if began is None
            else round((time.perf_counter() - began) * 1000, 3),
            "outcome": outcome,
            "step": str(step.id),
        }
        if outcome == "error":
            extra["error_type"] = type(args["error"]).__name__
            logger.warning("step", extra=extra)
        else:
            logger.info("step", extra=extra)


__all__ = ["step_hooks"]
