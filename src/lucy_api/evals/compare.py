"""What changed since a previous run: regressions first.

Scenarios are compared per model by pass rate over the runs that were not skipped, so a
single run and ``--repeat 5`` read the same way: 1.0 is passing, anything less is not.

* **regressions** -- passing before, not passing now. The reason this command exists.
* **fixes** -- not passing before, passing now.
* **moved** -- a partial pass rate that changed without crossing either line: flakiness
  getting better or worse.
* **skipped** -- a scenario that ran before and was skipped now, or the other way round.
  Losing coverage silently is its own kind of regression.
* **new** and **removed** -- in one report and not the other.
* **changed** -- the scenario *file* differs (by digest), so a changed verdict may be the
  scenario's doing rather than the hub's.
* **checks** -- every individual check whose pass rate moved, so the regression names the
  expectation that broke.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from lucy_api.evals.report import FORMAT, JSON_NAME, VERSION
from lucy_api.evals.results import PASSED, SKIPPED

if TYPE_CHECKING:
    from pathlib import Path

Key = tuple[str, str]
"""``(model, scenario)``."""


class CompareError(ValueError):
    """A previous report that cannot be compared against, and why."""


def load_previous(path: Path) -> dict[str, Any]:
    """A previous ``report.json``, or the directory holding one, checked before any run."""
    target = path / JSON_NAME if path.is_dir() else path
    try:
        report = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        message = f"cannot read {target}: {exc.strerror or exc}"
        raise CompareError(message) from exc
    except ValueError as exc:
        message = f"{target} is not JSON: {exc}"
        raise CompareError(message) from exc
    if not isinstance(report, dict) or report.get("format") != FORMAT:
        message = f"{target} is not a lucy eval report"
        raise CompareError(message)
    if report.get("version") != VERSION:
        message = (
            f"{target} is report version {report.get('version')!r}; this lucy reads version "
            f"{VERSION}. Compare with a report written by the same version."
        )
        raise CompareError(message)
    if not isinstance(report.get("runs"), list):
        message = f"{target} has no runs to compare"
        raise CompareError(message)
    return report


def compare(previous: dict[str, Any], current: dict[str, Any], *, label: str) -> dict[str, Any]:
    """The differences, as a JSON-ready document named after the previous report."""
    before = _standings(previous["runs"])
    after = _standings(current["runs"])
    shared = sorted(before.keys() & after.keys())
    result: dict[str, Any] = {
        "previous": label,
        "previous_started_at": previous.get("started_at"),
        "regressions": [],
        "fixes": [],
        "moved": [],
        "skipped": [],
        "new": [_row(key) for key in sorted(after.keys() - before.keys())],
        "removed": [_row(key) for key in sorted(before.keys() - after.keys())],
        "changed": [],
        "checks": _checks(previous["runs"], current["runs"]),
    }
    for key in shared:
        old, new = before[key], after[key]
        if old["digest"] != new["digest"]:
            result["changed"].append(_row(key))
        bucket = _bucket(old["rate"], new["rate"])
        if bucket:
            result[bucket].append(
                {**_row(key), "before": _verdict(old["rate"]), "after": _verdict(new["rate"])}
            )
    return result


def _bucket(old: float | None, new: float | None) -> str:
    """Which list a changed pass rate belongs in, or nothing when it did not change."""
    if old == new:
        return ""
    if old is None or new is None:
        return "skipped"
    if old == 1.0:
        return "regressions"
    if new == 1.0:
        return "fixes"
    return "moved"


def _standings(runs: list[dict[str, Any]]) -> dict[Key, dict[str, Any]]:
    grouped: dict[Key, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        grouped[(str(run["model"]), str(run["scenario"]))].append(run)
    standings: dict[Key, dict[str, Any]] = {}
    for key, group in grouped.items():
        counted = [run for run in group if run["outcome"] != SKIPPED]
        passed = sum(1 for run in counted if run["outcome"] == PASSED)
        standings[key] = {
            "rate": passed / len(counted) if counted else None,
            "digest": group[-1].get("digest"),
        }
    return standings


def _checks(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every check present in both reports whose pass rate moved."""
    before, after = _check_rates(previous), _check_rates(current)
    moved: list[dict[str, Any]] = []
    for key in sorted(before.keys() & after.keys()):
        (was, of), (now, out_of) = before[key], after[key]
        if was * out_of != now * of:
            model, scenario, name = key
            moved.append(
                {
                    "model": model,
                    "scenario": scenario,
                    "check": name,
                    "before": _fraction(before[key]),
                    "after": _fraction(after[key]),
                }
            )
    return moved


def _check_rates(runs: list[dict[str, Any]]) -> dict[tuple[str, str, str], tuple[int, int]]:
    tallies: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for run in runs:
        for turn in run["turns"]:
            for check in turn["checks"]:
                tally = tallies[(str(run["model"]), str(run["scenario"]), str(check["name"]))]
                tally[0] += int(bool(check["passed"]))
                tally[1] += 1
    return {key: (passed, total) for key, (passed, total) in tallies.items()}


def _row(key: Key) -> dict[str, str]:
    return {"model": key[0], "scenario": key[1]}


def _verdict(rate: float | None) -> str:
    if rate is None:
        return "skipped"
    if rate == 1.0:
        return "passed"
    if rate == 0.0:
        return "failed"
    return f"passed {rate:.0%} of runs"


def _fraction(tally: tuple[int, int]) -> str:
    return f"{tally[0]}/{tally[1]}"


__all__ = ["CompareError", "compare", "load_previous"]
