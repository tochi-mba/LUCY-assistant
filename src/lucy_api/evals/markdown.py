"""``report.md``: the run as a person reads it, failures first.

Rendered from the JSON document rather than from records, so any ``report.json`` -- this
run's or one kept from last month -- reads the same way.

Two rules from the rest of the codebase apply here. **Nothing is cut silently**: a long
reply is shown up to a limit with the exact count of what is not shown, and the full text
is in ``report.json``. **Model output is never allowed to break the document**: replies go
in fences longer than any run of backticks inside them, and table cells have their pipes
and line breaks escaped, because a regression that renders as a mangled table is a
regression nobody sees.
"""

from __future__ import annotations

import re
from typing import Any

from lucy_api.evals.report import flaky_checks
from lucy_api.evals.results import ERROR, FAILED, SKIPPED

TEXT_LIMIT = 2_000
"""Characters of a message shown in a transcript excerpt."""

CELL_LIMIT = 200
"""Characters of a detail shown in a table cell."""

FENCE_MINIMUM = 3
MINUTE = 60
SEVERAL = 2
"""Columns or runs at which a scenario-by-model table starts saying something."""
LABELS = {"passed": "pass", "failed": "FAIL", "skipped": "skip", "error": "ERROR"}


def render(report: dict[str, Any]) -> str:
    """The whole document."""
    sections = (
        _header(report),
        _summary(report),
        _matrix(report),
        _failures(report),
        _flaky(report),
        _comparison(report.get("comparison")),
        _skipped(report),
        _every_run(report),
    )
    return "\n".join(line for section in sections for line in section).rstrip() + "\n"


def _header(report: dict[str, Any]) -> list[str]:
    plan = report["plan"]
    environment = report["environment"]
    runs = report["runs"]
    failing = [run for run in runs if run["outcome"] in {FAILED, ERROR}]
    skipped = sum(1 for run in runs if run["outcome"] == SKIPPED)
    if report["stopped"]:
        verdict = f"**The run stopped early:** {report['stopped']}"
    elif failing:
        verdict = f"**{len(failing)} of {len(runs)} runs did not pass.**"
    elif runs and skipped == len(runs):
        verdict = "**Nothing ran:** every scenario was skipped."
    else:
        verdict = "**Every check passed.**"
    if skipped and not report["stopped"]:
        verdict += f" {skipped} skipped."
    hub = environment.get("hub", {})
    rows = (
        ("Hub", f"{hub.get('url', '')} (version {hub.get('version') or 'unknown'})"),
        ("Models", ", ".join(plan["models"])),
        ("Profile", plan["profile"]),
        ("Scenarios", f"{len(plan['scenarios'])}, each run {plan['repeat']} time(s)"),
        ("Prompts", str(plan["prompts"])),
        ("Took", _duration(report["seconds"])),
        ("Client", f"lucy {environment.get('client', {}).get('version', '')}"),
    )
    lines = [f"# Lucy eval, {report['started_at']}", "", verdict, "", "| | |", "| --- | --- |"]
    lines.extend(f"| {name} | {_cell(value)} |" for name, value in rows)
    return [*lines, ""]


def _summary(report: dict[str, Any]) -> list[str]:
    lines = [
        "## Summary",
        "",
        "| Model | Passed | Failed | Errors | Skipped | Checks | Median turn | "
        "Tokens in / out / cache read |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, row in report["summary"].items():
        median = row["median_turn_seconds"]
        tokens = (
            f"{row['input_tokens']:,} / {row['output_tokens']:,} / {row['cache_read_tokens']:,}"
        )
        lines.append(
            f"| {_cell(model)} | {row['passed']} | {row['failed']} | {row['error']} | "
            f"{row['skipped']} | {row['checks_passed']}/{row['checks_total']} | "
            f"{_duration(median) if median is not None else '-'} | {tokens} |"
        )
    return [*lines, ""]


def _matrix(report: dict[str, Any]) -> list[str]:
    """Scenario by model, when there is more than one column or more than one run."""
    plan = report["plan"]
    models = plan["models"]
    if len(models) < SEVERAL and plan["repeat"] < SEVERAL:
        return []
    lines = [
        "## By scenario",
        "",
        "| Scenario | " + " | ".join(_cell(model) for model in models) + " |",
        "| --- | " + " | ".join("---" for _ in models) + " |",
    ]
    for scenario in plan["scenarios"]:
        cells = [_standing(report["runs"], model, scenario) for model in models]
        lines.append(f"| {_cell(scenario)} | " + " | ".join(cells) + " |")
    return [*lines, ""]


def _standing(runs: list[dict[str, Any]], model: str, scenario: str) -> str:
    mine = [run for run in runs if run["model"] == model and run["scenario"] == scenario]
    outcomes = {run["outcome"] for run in mine}
    if len(outcomes) == 1:
        return LABELS[outcomes.pop()]
    counted = [run for run in mine if run["outcome"] != SKIPPED]
    passed = sum(1 for run in counted if run["outcome"] == "passed")
    return f"{passed}/{len(counted)}" if counted else "-"


def _failures(report: dict[str, Any]) -> list[str]:
    failing = [run for run in report["runs"] if run["outcome"] in {FAILED, ERROR}]
    if not failing:
        return []
    lines = ["## Failures", ""]
    for run in failing:
        lines.extend(_failure(run))
    return lines


def _failure(run: dict[str, Any]) -> list[str]:
    session = f", session `{run['session_id']}`" if run["session_id"] else ""
    lines = [
        f"### {run['scenario']} with {run['model']}, run {run['repeat']}",
        "",
        run["summary"],
        "",
        f"**{run['outcome']}** after {_duration(run['seconds'])}{session}.",
    ]
    if run["reason"]:
        lines.append(f"Why: {run['reason']}")
    failing = [check for turn in run["turns"] for check in turn["checks"] if not check["passed"]]
    if failing:
        lines.extend(["", "Failing checks:", ""])
        lines.extend(f"- `{check['name']}`: {check['detail']}" for check in failing)
    if run["seed"]:
        lines.extend(["", "Seeded:", ""])
        lines.extend(_invocations(run["seed"]))
    lines.append("")
    for turn in run["turns"]:
        if not all(check["passed"] for check in turn["checks"]):
            lines.extend(_turn(turn))
    return lines


def _turn(turn: dict[str, Any]) -> list[str]:
    title = (
        f"#### Turn {turn['index']}: {turn['status'] or 'unknown'} in "
        f"{_duration(turn['seconds'])}, {turn['iterations']} round(s), "
        f"{len(turn['results'])} step(s)"
    )
    lines = [title, "", "Person:", "", *_fenced(turn["said"]), "", "Lucy:", ""]
    lines.extend(_fenced(turn["reply"]) if turn["reply"] else ["*(no reply)*"])
    lines.append("")
    if turn["results"]:
        lines.extend(["| Operation | Status | Detail |", "| --- | --- | --- |"])
        lines.extend(
            f"| {_cell(result['operation'])} | {_cell(result['status'])} | "
            f"{_cell(_clip(result['error'] or result['note'], CELL_LIMIT))} |"
            for result in turn["results"]
        )
        lines.append("")
    for ask in turn["asks"]:
        wanted = f": {ask['arguments']}" if ask["arguments"] else ""
        lines.append(
            f"- Asked to approve {ask['operation']} ({ask['answer'] or 'unanswered'}){wanted}"
        )
    lines.extend(f"- The transcript recorded {error}" for error in turn["errors"])
    if turn["verify"]:
        lines.extend(["", "Verified:", ""])
        lines.extend(_invocations(turn["verify"]))
    return [*lines, ""]


def _invocations(records: list[dict[str, Any]]) -> list[str]:
    lines = ["| Operation | Status | Output or error |", "| --- | --- | --- |"]
    lines.extend(
        f"| {_cell(record['op'])} | {_cell(record['status'])} | "
        f"{_cell(_clip(record['error'] or record['output'], CELL_LIMIT))} |"
        for record in records
    )
    return lines


def _flaky(report: dict[str, Any]) -> list[str]:
    found = flaky_checks(report)
    if not found:
        return []
    lines = [
        "## Flaky checks",
        "",
        "Checks that held on some runs and not others.",
        "",
        "| Model | Scenario | Check | Held |",
        "| --- | --- | --- | ---: |",
    ]
    lines.extend(
        f"| {_cell(model)} | {_cell(scenario)} | {_cell(name)} | {passed}/{runs} |"
        for model, scenario, name, passed, runs in found
    )
    return [*lines, ""]


def _comparison(comparison: dict[str, Any] | None) -> list[str]:
    if not comparison:
        return []
    lines = [f"## Compared with {comparison['previous']}", ""]
    regressions = comparison["regressions"]
    lines.append(f"**{len(regressions)} regression(s).**" if regressions else "No regressions.")
    lines.append("")
    for title, key in (
        ("Regressions", "regressions"),
        ("Fixes", "fixes"),
        ("Pass rate moved", "moved"),
        ("Skipped in one run and not the other", "skipped"),
    ):
        rows = comparison[key]
        if rows:
            lines.extend([f"{title}:", ""])
            lines.extend(
                f"- {row['scenario']} with {row['model']}: {row['before']} -> {row['after']}"
                for row in rows
            )
            lines.append("")
    for title, key in (
        ("New scenarios", "new"),
        ("Removed scenarios", "removed"),
        ("Scenario files that changed", "changed"),
    ):
        rows = comparison[key]
        if rows:
            lines.extend([f"{title}:", ""])
            lines.extend(f"- {row['scenario']} with {row['model']}" for row in rows)
            lines.append("")
    if comparison["checks"]:
        lines.extend(
            ["| Model | Scenario | Check | Before | After |", "| --- | --- | --- | ---: | ---: |"]
        )
        lines.extend(
            f"| {_cell(row['model'])} | {_cell(row['scenario'])} | {_cell(row['check'])} | "
            f"{row['before']} | {row['after']} |"
            for row in comparison["checks"]
        )
        lines.append("")
    return lines


def _skipped(report: dict[str, Any]) -> list[str]:
    skipped = [run for run in report["runs"] if run["outcome"] == SKIPPED]
    if not skipped:
        return []
    lines = ["## Skipped", "", "| Scenario | Model | Why |", "| --- | --- | --- |"]
    lines.extend(
        f"| {_cell(run['scenario'])} | {_cell(run['model'])} | {_cell(run['reason'])} |"
        for run in skipped
    )
    return [*lines, ""]


def _every_run(report: dict[str, Any]) -> list[str]:
    lines = [
        "## Every run",
        "",
        "| Scenario | Model | Run | Outcome | Checks | Took | Session |",
        "| --- | --- | ---: | --- | ---: | ---: | --- |",
    ]
    lines.extend(
        f"| {_cell(run['scenario'])} | {_cell(run['model'])} | {run['repeat']} | "
        f"{LABELS[run['outcome']]} | {run['checks_passed']}/{run['checks_total']} | "
        f"{_duration(run['seconds'])} | {_cell(run['session_id'] or '-')} |"
        for run in report["runs"]
    )
    return [*lines, ""]


def _fenced(text: str) -> list[str]:
    """A fence no run of backticks inside the text can close."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(FENCE_MINIMUM, longest + 1)
    return [f"{fence}text", _clip(text, TEXT_LIMIT), fence]


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]} [showing {limit:,} of {len(text):,} characters; report.json has all]"


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _duration(seconds: float) -> str:
    if seconds < MINUTE:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), MINUTE)
    return f"{minutes}m {rest:02d}s"


__all__ = ["render"]
