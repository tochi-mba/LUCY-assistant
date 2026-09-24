"""Comparing a run with a previous one: regressions first, and nothing misread.

A comparison is only worth reading if it cannot be wrong about which report it was given,
so a previous report is validated before the run starts -- a mistyped path must cost a
second, not an hour of conversations.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.evals.compare import CompareError, compare, load_previous
from lucy_api.evals.report import FORMAT, JSON_NAME, VERSION

if TYPE_CHECKING:
    from pathlib import Path


def run(
    scenario: str,
    outcome: str,
    *checks: tuple[str, bool],
    model: str = "clyde:haiku",
    digest: str = "d1",
) -> dict[str, Any]:
    return {
        "model": model,
        "scenario": scenario,
        "outcome": outcome,
        "digest": digest,
        "turns": [{"checks": [{"name": name, "passed": passed} for name, passed in checks]}],
    }


def document(*runs: dict[str, Any]) -> dict[str, Any]:
    return {"format": FORMAT, "version": VERSION, "started_at": "then", "runs": list(runs)}


# --------------------------------------------------------------------------------------
# Reading the previous report
# --------------------------------------------------------------------------------------


def test_a_previous_report_is_read_from_its_file_or_its_folder(tmp_path: Path) -> None:
    saved = document(run("default/a", "passed"))
    (tmp_path / JSON_NAME).write_text(json.dumps(saved), encoding="utf-8")
    assert load_previous(tmp_path) == saved
    assert load_previous(tmp_path / JSON_NAME) == saved


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        ("{not json", "is not JSON"),
        ("[]", "is not a lucy eval report"),
        (json.dumps({"format": "something-else"}), "is not a lucy eval report"),
        (json.dumps({"format": FORMAT, "version": 99}), "is report version 99"),
        (json.dumps({"format": FORMAT, "version": VERSION}), "has no runs to compare"),
    ],
)
def test_a_report_that_cannot_be_compared_says_why(
    tmp_path: Path, content: str, complaint: str
) -> None:
    path = tmp_path / "report.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(CompareError, match=complaint):
        load_previous(path)


def test_a_missing_report_is_named(tmp_path: Path) -> None:
    with pytest.raises(CompareError, match="cannot read"):
        load_previous(tmp_path / "nowhere.json")


# --------------------------------------------------------------------------------------
# What changed
# --------------------------------------------------------------------------------------


def test_every_kind_of_change_lands_in_its_own_list() -> None:
    before = document(
        run("default/regressed", "passed", ("x", True)),
        run("default/fixed", "failed", ("y", False)),
        run("default/flaky", "passed", ("z", True)),
        run("default/flaky", "failed", ("z", False)),
        run("default/flaky", "failed", ("z", False)),
        run("default/now-skipped", "passed"),
        run("default/was-skipped", "skipped"),
        run("default/steady", "passed", ("s", True), digest="old"),
        run("default/gone", "passed"),
    )
    after = document(
        run("default/regressed", "failed", ("x", False)),
        run("default/fixed", "passed", ("y", True)),
        run("default/flaky", "passed", ("z", True)),
        run("default/flaky", "passed", ("z", True)),
        run("default/flaky", "error", ("z", False)),
        run("default/now-skipped", "skipped"),
        run("default/was-skipped", "passed"),
        run("default/steady", "passed", ("s", True), digest="new"),
        run("default/arrived", "passed"),
    )

    result = compare(before, after, label="old/report.json")

    def rows(key: str) -> list[tuple[str, ...]]:
        return [tuple(str(value) for value in row.values()) for row in result[key]]

    assert result["previous"] == "old/report.json"
    assert result["previous_started_at"] == "then"
    assert rows("regressions") == [("clyde:haiku", "default/regressed", "passed", "failed")]
    assert rows("fixes") == [("clyde:haiku", "default/fixed", "failed", "passed")]
    assert rows("moved") == [
        ("clyde:haiku", "default/flaky", "passed 33% of runs", "passed 67% of runs")
    ]
    assert rows("skipped") == [
        ("clyde:haiku", "default/now-skipped", "passed", "skipped"),
        ("clyde:haiku", "default/was-skipped", "skipped", "passed"),
    ]
    assert rows("new") == [("clyde:haiku", "default/arrived")]
    assert rows("removed") == [("clyde:haiku", "default/gone")]
    assert rows("changed") == [("clyde:haiku", "default/steady")]
    assert result["checks"] == [
        {
            "model": "clyde:haiku",
            "scenario": "default/fixed",
            "check": "y",
            "before": "0/1",
            "after": "1/1",
        },
        {
            "model": "clyde:haiku",
            "scenario": "default/flaky",
            "check": "z",
            "before": "1/3",
            "after": "2/3",
        },
        {
            "model": "clyde:haiku",
            "scenario": "default/regressed",
            "check": "x",
            "before": "1/1",
            "after": "0/1",
        },
    ]


def test_the_same_pass_rate_over_more_runs_is_not_a_change() -> None:
    before = document(run("default/a", "passed", ("c", True)))
    after = document(
        run("default/a", "passed", ("c", True)),
        run("default/a", "passed", ("c", True)),
    )
    result = compare(before, after, label="old")
    assert result["checks"] == []
    assert result["moved"] == result["regressions"] == result["fixes"] == []


def test_models_are_compared_separately() -> None:
    before = document(run("default/a", "passed", model="clyde:haiku"))
    after = document(
        run("default/a", "passed", model="clyde:haiku"),
        run("default/a", "failed", model="clyde:sonnet"),
    )
    result = compare(before, after, label="old")
    assert result["regressions"] == []
    assert result["new"] == [{"model": "clyde:sonnet", "scenario": "default/a"}]
