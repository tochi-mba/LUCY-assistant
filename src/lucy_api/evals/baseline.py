"""A report cut down to its measurements, small enough to commit and compare against.

A full report carries every reply, every step's result and every approval, which is what
reading a failure needs and what a repository does not. A baseline keeps what a later run
is measured against -- each scenario's outcome, every check by name, and the tokens,
rounds and seconds each turn spent, plus the fixed prompt's size -- in the same format, so
`lucy eval run --compare docs/baselines/<name>.json` reads it like any other report.
"""

from __future__ import annotations

from typing import Any

TURN_FIELDS = (
    "index",
    "status",
    "termination",
    "seconds",
    "timed_out",
    "iterations",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
)
RUN_FIELDS = (
    "scenario",
    "suite",
    "name",
    "digest",
    "model",
    "repeat",
    "outcome",
    "reason",
    "seconds",
    "checks_passed",
    "checks_total",
)
REPORT_FIELDS = (
    "format",
    "version",
    "started_at",
    "finished_at",
    "seconds",
    "environment",
    "plan",
    "stopped",
    "passed",
    "summary",
    "prompt",
)


def trimmed(report: dict[str, Any], *, label: str, source: str) -> dict[str, Any]:
    """The measurements of `report`, with no reply, result or session id in them."""
    baseline: dict[str, Any] = {key: report[key] for key in REPORT_FIELDS if key in report}
    plan = baseline.get("plan")
    if isinstance(plan, dict):
        # The profile is a per-run name and says nothing a later run can be measured by.
        baseline["plan"] = {key: value for key, value in plan.items() if key != "profile"}
    baseline["baseline"] = {"label": label, "from": source}
    baseline["runs"] = [_run(run) for run in report.get("runs") or () if isinstance(run, dict)]
    baseline["comparison"] = None
    return baseline


def _run(run: dict[str, Any]) -> dict[str, Any]:
    kept = {key: run[key] for key in RUN_FIELDS if key in run}
    kept["turns"] = [_turn(turn) for turn in run.get("turns") or () if isinstance(turn, dict)]
    return kept


def _turn(turn: dict[str, Any]) -> dict[str, Any]:
    kept = {key: turn[key] for key in TURN_FIELDS if key in turn}
    kept["checks"] = [
        {"name": check.get("name"), "passed": bool(check.get("passed"))}
        for check in turn.get("checks") or ()
        if isinstance(check, dict)
    ]
    return kept


__all__ = ["trimmed"]
