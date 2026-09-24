"""The run, written down: ``report.json`` for a machine, ``report.md`` for a person.

The JSON is the contract -- versioned, so ``--compare`` can refuse a report it does not
understand instead of misreading it -- and it holds everything: every turn's full reply,
every tool result, every check with its evidence. The Markdown is a reading of it that
puts failures first, because the one question somebody opening it has is "what broke".

Pass rates are computed per check, not only per scenario. With ``--repeat`` a scenario that
passes two runs in three is not "passed" and not "failed"; it is flaky, and which *check*
flickers is the thing worth knowing.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from lucy_api.evals.results import ERROR, FAILED, OUTCOMES, PASSED, SKIPPED

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime
    from pathlib import Path

    from lucy_api.evals.results import ScenarioRecord
    from lucy_api.evals.runner import Plan

FORMAT = "lucy-eval-report"
VERSION = 1
JSON_NAME = "report.json"
MARKDOWN_NAME = "report.md"


def build_report(  # noqa: PLR0913 - one argument per section of the document
    *,
    plan: Plan,
    records: Iterable[ScenarioRecord],
    environment: dict[str, Any],
    started: datetime,
    finished: datetime,
    stopped: str = "",
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole run as one JSON-ready document."""
    kept = list(records)
    runs = [record.to_dict() for record in kept]
    failing = any(record.outcome in {FAILED, ERROR} for record in kept)
    return {
        "format": FORMAT,
        "version": VERSION,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
        "seconds": round((finished - started).total_seconds(), 3),
        "environment": environment,
        "plan": {
            "models": list(plan.models),
            "profile": plan.profile,
            "repeat": plan.repeat,
            "timeout_seconds": plan.timeout,
            "keep_sessions": plan.keep_sessions,
            "scenarios": [scenario.qualified for scenario in plan.scenarios],
            "prompts": plan.prompts,
            "selection": selection or {},
        },
        "stopped": stopped,
        "passed": not failing and not stopped,
        "summary": summarize(runs, plan.models),
        "pass_rates": pass_rates(runs),
        "runs": runs,
        "comparison": None,
    }


def write_report(directory: Path, report: dict[str, Any], *, markdown: str) -> tuple[Path, Path]:
    """Both files, into a directory that already exists. Returns their paths."""
    as_json = directory / JSON_NAME
    as_markdown = directory / MARKDOWN_NAME
    as_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    as_markdown.write_text(markdown, encoding="utf-8")
    return as_json, as_markdown


def summarize(runs: list[dict[str, Any]], models: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Per model: how many runs landed in each outcome, how many checks held, and the cost."""
    summary: dict[str, dict[str, Any]] = {}
    for model in models:
        mine = [run for run in runs if run["model"] == model]
        turns = [turn for run in mine for turn in run["turns"]]
        durations = [float(turn["seconds"]) for turn in turns]
        row: dict[str, Any] = dict.fromkeys(OUTCOMES, 0)
        for run in mine:
            row[run["outcome"]] += 1
        row.update(
            runs=len(mine),
            checks_passed=sum(run["checks_passed"] for run in mine),
            checks_total=sum(run["checks_total"] for run in mine),
            turns=len(turns),
            median_turn_seconds=round(statistics.median(durations), 3) if durations else None,
            input_tokens=sum(int(turn["input_tokens"]) for turn in turns),
            output_tokens=sum(int(turn["output_tokens"]) for turn in turns),
            cache_read_tokens=sum(int(turn["cache_read_tokens"] or 0) for turn in turns),
        )
        summary[model] = row
    return summary


def pass_rates(runs: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Model, then scenario: runs, passes, and each check's passes over the runs it was in.

    A skipped run is left out of every denominator: it says nothing about the model.
    """
    rates: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for run in runs:
        if run["outcome"] == SKIPPED:
            continue
        entry = rates[run["model"]].setdefault(
            run["scenario"], {"runs": 0, "passed": 0, "checks": {}}
        )
        entry["runs"] += 1
        entry["passed"] += int(run["outcome"] == PASSED)
        for turn in run["turns"]:
            for check in turn["checks"]:
                tally = entry["checks"].setdefault(check["name"], {"runs": 0, "passed": 0})
                tally["runs"] += 1
                tally["passed"] += int(check["passed"])
    return dict(rates)


def flaky_checks(report: dict[str, Any]) -> list[tuple[str, str, str, int, int]]:
    """``(model, scenario, check, passed, runs)`` for every check that both held and did not."""
    found: list[tuple[str, str, str, int, int]] = []
    for model, scenarios in report["pass_rates"].items():
        for scenario, entry in scenarios.items():
            for name, tally in entry["checks"].items():
                if 0 < tally["passed"] < tally["runs"]:
                    found.append((model, scenario, name, tally["passed"], tally["runs"]))
    return found


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


__all__ = [
    "FORMAT",
    "JSON_NAME",
    "MARKDOWN_NAME",
    "VERSION",
    "build_report",
    "flaky_checks",
    "pass_rates",
    "summarize",
    "write_report",
]
