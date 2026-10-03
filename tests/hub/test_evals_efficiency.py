"""What a change cost or saved, and a baseline small enough to commit and measure against.

Pass rates say Lucy still does the job; these pin that a run also says what the job took --
tokens, model rounds, seconds and the fixed prompt's size -- next to a previous run, and
that `lucy eval baseline` keeps exactly those numbers and nothing a repository should not
hold.
"""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from eval_fakes import Clock, FakeLucy, FakeShell, Play

from lucy_api.cli import evals as command
from lucy_api.cli.base import OK, REFUSED, TOKEN_VAR, USAGE
from lucy_api.cli.main import main as cli_main
from lucy_api.evals.baseline import trimmed
from lucy_api.evals.compare import compare, load_previous
from lucy_api.evals.conversation import Pace
from lucy_api.evals.efficiency import (
    delta,
    efficiency,
    measures,
    prompt_change,
    summary_lines,
)
from lucy_api.evals.markdown import _comparison
from lucy_api.evals.report import JSON_NAME, MARKDOWN_NAME

SUITE = {
    "greeting.toml": 'summary = "Says hello."\n'
    '[[turns]]\nsay = "Hello?"\n[turns.expect]\nreply_matches = ["hello"]\n',
}


def a_turn(**overrides: Any) -> dict[str, Any]:
    turn: dict[str, Any] = {
        "index": 1,
        "said": "private words",
        "status": "completed",
        "termination": "success",
        "seconds": 10.0,
        "timed_out": False,
        "iterations": 2,
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_read_tokens": 50,
        "reply": "a reply nobody should commit",
        "results": [{"operation": "notes.search", "output": "secret"}],
        "checks": [{"name": "reply is not empty", "passed": True, "detail": "x"}],
    }
    return {**turn, **overrides}


def a_run(scenario: str = "s/one", *, outcome: str = "passed", **turn: Any) -> dict[str, Any]:
    return {
        "scenario": scenario,
        "suite": "s",
        "name": scenario.rsplit("/", maxsplit=1)[-1],
        "digest": "d1",
        "model": "clyde:haiku",
        "repeat": 1,
        "outcome": outcome,
        "reason": "",
        "session_id": "ses_private",
        "seconds": 12.0,
        "seed": ["planted"],
        "turns": [a_turn(**turn)],
        "usage": {"input_tokens": 1},
        "checks_passed": 1,
        "checks_total": 1,
    }


def a_report(*runs: dict[str, Any], prompt: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "format": "lucy-eval-report",
        "version": 1,
        "started_at": "2026-10-02T15:31:38Z",
        "finished_at": "2026-10-02T15:49:37Z",
        "seconds": 1.0,
        "environment": {"hub": {"url": "http://127.0.0.1:8000"}},
        "plan": {"models": ["clyde:haiku"], "profile": "eval-20261002", "repeat": 1},
        "stopped": "",
        "passed": True,
        "summary": {},
        "pass_rates": {},
        "runs": list(runs),
        "comparison": None,
        "prompt": prompt,
    }


# --------------------------------------------------------------------------------------
# Measuring
# --------------------------------------------------------------------------------------


def test_a_run_is_measured_from_its_turns() -> None:
    run = a_run()
    run["turns"].append(a_turn(input_tokens=500, iterations=1))
    run["turns"].append("not a turn")
    assert measures(run) == {
        "input_tokens": 1_500.0,
        "output_tokens": 200.0,
        "cache_read_tokens": 100.0,
        "rounds": 3.0,
        "seconds": 12.0,
        "turns": 2.0,
    }
    assert measures({"turns": [{"input_tokens": True, "iterations": "2"}]})["rounds"] == 0.0


def test_spend_is_compared_per_scenario_and_per_model_over_shared_scenarios() -> None:
    before = [
        a_run("s/one", input_tokens=1_000),
        a_run("s/two", input_tokens=3_000),
        a_run("s/gone", input_tokens=99_999),
    ]
    after = [
        a_run("s/one", input_tokens=800),
        a_run("s/one", input_tokens=600),
        a_run("s/two", input_tokens=3_000, outcome="failed"),
        a_run("s/new", input_tokens=99_999),
        a_run("s/broke", outcome="error"),
        a_run("s/skip", outcome="skipped"),
    ]
    spent = efficiency(before, after)

    one = next(row for row in spent["scenarios"] if row["scenario"] == "s/one")
    assert one["input_tokens"] == {"before": 1_000.0, "after": 700.0, "change": -30.0}
    totals = spent["models"]["clyde:haiku"]
    assert totals["scenarios"] == 2, "only what both runs held is compared"
    assert totals["input_tokens"] == {"before": 4_000.0, "after": 3_700.0, "change": -7.5}
    assert [row["scenario"] for row in spent["scenarios"]] == ["s/one", "s/two"]


def test_nothing_spent_before_has_no_percentage() -> None:
    assert delta(0, 5) == {"before": 0, "after": 5, "change": None}


def test_the_fixed_prompt_is_compared_section_by_section() -> None:
    old = {"total": 6_000, "sections": {"identity": 300, "tools": 1_100, "gone": 20}}
    new = {"total": 5_500, "sections": {"identity": 300, "tools": 1_000, "added": 40, "bad": "x"}}
    change = prompt_change(old, new)
    assert change is not None
    assert change["total"] == {"before": 6_000.0, "after": 5_500.0, "change": -8.3}
    assert change["sections"] == [
        {"section": "added", "before": 0, "after": 40},
        {"section": "gone", "before": 20, "after": 0},
        {"section": "tools", "before": 1_100, "after": 1_000},
    ]
    assert prompt_change(None, new) is None
    assert prompt_change(old, "nope") is None
    assert prompt_change({"sections": []}, {"sections": None})["sections"] == []  # type: ignore[index]


def test_the_comparison_carries_spend_and_the_prompt_and_says_it_in_a_few_lines() -> None:
    previous = a_report(a_run(input_tokens=1_000), prompt={"total": 6_000, "sections": {}})
    current = a_report(a_run(input_tokens=900), prompt={"total": 5_400, "sections": {}})
    comparison = compare(previous, current, label="docs/baselines/clyde-haiku.json")

    assert comparison["efficiency"]["models"]["clyde:haiku"]["input_tokens"]["change"] == -10.0
    lines = summary_lines(comparison)
    assert lines[0] == "  fixed prompt tokens 6,000 -> 5,400 (-10.0%)"
    assert lines[1].startswith("  clyde:haiku over 1 shared scenario(s): input tokens 1,000 -> 900")
    assert "seconds 12 -> 12 (+0.0%)" in lines[1]
    assert summary_lines({"efficiency": None, "prompt": None}) == []


def test_the_markdown_report_shows_what_the_run_spent_next_to_the_last_one() -> None:
    previous = a_report(a_run(input_tokens=1_000), prompt={"total": 6_000, "sections": {"a": 1}})
    current = a_report(a_run(input_tokens=900), prompt={"total": 5_400, "sections": {"a": 2}})
    comparison = compare(previous, current, label="before")
    text = "\n".join(_comparison(comparison))
    assert "| **total** | 6,000 | 5,400 |" in text
    assert "| a | 1 | 2 |" in text
    assert "| clyde:haiku | **all shared** | 1,000 -> 900 (-10.0%) |" in text
    assert "| clyde:haiku | s/one |" in text

    empty = "\n".join(_comparison(compare(a_report(), a_report(), label="empty")))
    assert "**all shared**" not in empty
    assert "Fixed prompt:" not in empty


# --------------------------------------------------------------------------------------
# Baselines
# --------------------------------------------------------------------------------------


def test_a_baseline_keeps_the_numbers_and_nothing_private() -> None:
    report = a_report(a_run(), prompt={"total": 6_000, "sections": {}})
    baseline = trimmed(report, label="after the compaction fixes", source="20261002T153114Z")
    text = json.dumps(baseline)

    for private in ("private words", "a reply nobody", "secret", "ses_private", "planted"):
        assert private not in text, private
    assert "eval-20261002" not in text, "the run's profile name is not a measurement"
    assert baseline["baseline"] == {
        "label": "after the compaction fixes",
        "from": "20261002T153114Z",
    }
    [turn] = baseline["runs"][0]["turns"]
    assert turn["input_tokens"] == 1_000
    assert turn["checks"] == [{"name": "reply is not empty", "passed": True}]
    assert baseline["prompt"] == {"total": 6_000, "sections": {}}
    assert baseline["comparison"] is None


def test_a_baseline_is_read_and_compared_like_any_report(tmp_path: Path) -> None:
    saved = tmp_path / "baseline.json"
    saved.write_text(json.dumps(trimmed(a_report(a_run()), label="b", source="r")), "utf-8")
    previous = load_previous(saved)
    comparison = compare(previous, a_report(a_run(outcome="failed")), label=str(saved))
    assert comparison["regressions"][0]["scenario"] == "s/one"
    assert comparison["efficiency"]["models"]["clyde:haiku"]["scenarios"] == 1


def test_a_baseline_survives_a_report_with_odd_shapes() -> None:
    report = a_report(a_run())
    report["plan"] = "not a plan"
    report["runs"].append("not a run")
    report["runs"][0]["turns"][0]["checks"].append("not a check")
    baseline = trimmed(report, label="b", source="r")
    assert baseline["plan"] == "not a plan"
    assert len(baseline["runs"]) == 1
    assert baseline["runs"][0]["turns"][0]["checks"] == [
        {"name": "reply is not empty", "passed": True}
    ]


# --------------------------------------------------------------------------------------
# lucy eval baseline, and the prompt on every run
# --------------------------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLucy:
    hub = FakeLucy()
    hub.say("Hello?", Play(reply="Hello there."))
    monkeypatch.setattr(httpx, "Client", lambda **_: hub.client())
    return hub


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    clock = Clock()
    monkeypatch.setattr(command, "pace", lambda: Pace(clock=clock, sleep=clock.sleep))
    monkeypatch.setattr(command, "utc_now", lambda: datetime(2026, 10, 2, 15, 31, 14, tzinfo=UTC))
    shell = FakeShell()
    monkeypatch.setattr(command, "shell", lambda: shell)


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    folder = tmp_path / "mine"
    folder.mkdir()
    for name, text in SUITE.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def lucy(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environ = {"LUCY_CONFIG": str(Path.cwd() / "config.toml"), TOKEN_VAR: "t"}
    code = cli_main(["eval", *argv], out=out, err=err, in_=io.StringIO(), environ=environ)
    return code, out.getvalue(), err.getvalue()


def only_report(tmp_path: Path) -> Path:
    [folder] = sorted((tmp_path / "var" / "evals").iterdir())
    return folder


def test_every_run_records_the_fixed_prompt_it_was_held_with(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    assert lucy("run", "--model", "clyde:haiku", "--suite", str(suite))[0] == OK
    saved = json.loads((only_report(tmp_path) / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["prompt"] == {
        "version": "p1",
        "total": 6000,
        "bands": {"system": 5900, "pinned": 100},
        "sections": {"identity": 300, "tools": 1100},
    }


def test_a_hub_that_cannot_say_its_prompt_costs_only_that_figure(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    fake.prompt = None
    assert lucy("run", "--model", "clyde:haiku", "--suite", str(suite))[0] == OK
    saved = json.loads((only_report(tmp_path) / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["prompt"] is None


def test_an_odd_prompt_preview_is_recorded_without_its_sections(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    fake.prompt = {"version": "p2", "total": 10, "sections": "not a list"}
    assert lucy("run", "--model", "clyde:haiku", "--suite", str(suite))[0] == OK
    saved = json.loads((only_report(tmp_path) / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["prompt"]["sections"] == {}


def test_baseline_then_compare_says_what_the_next_run_spent(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    assert lucy("run", "--model", "clyde:haiku", "--suite", str(suite))[0] == OK
    folder = only_report(tmp_path)

    code, out, _ = lucy("baseline", str(folder), "--label", "before")
    assert code == OK
    assert f"baseline 'before': 1 run(s) written to {folder / 'baseline.json'}" in out
    target = tmp_path / "docs" / "baselines" / "clyde-haiku.json"
    code, out, _ = lucy("baseline", str(folder / JSON_NAME), "--out", str(target), "--json")
    assert code == OK
    assert json.loads(out)["label"] == "2026-10-02T15:31:14Z"
    kept = json.loads(target.read_text(encoding="utf-8"))
    assert "Hello there." not in json.dumps(kept)

    fake.say("Hello?", Play(reply="Goodbye."))
    code, out, _ = lucy(
        "run", "--model", "clyde:haiku", "--suite", str(suite), "--compare", str(target)
    )
    assert code == REFUSED
    assert "  fixed prompt tokens 6,000 -> 6,000 (+0.0%)" in out
    assert "  clyde:haiku over 1 shared scenario(s): input tokens" in out
    newest = sorted((tmp_path / "var" / "evals").iterdir())[-1]
    assert "**all shared**" in (newest / MARKDOWN_NAME).read_text(encoding="utf-8")


def test_a_baseline_from_something_that_is_not_a_report_is_refused(tmp_path: Path) -> None:
    (tmp_path / "junk.json").write_text("{}", encoding="utf-8")
    code, _, err = lucy("baseline", str(tmp_path / "junk.json"))
    assert code == USAGE
    assert "not a lucy eval report" in err


def test_a_baseline_that_cannot_be_written_says_where(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps(a_report(a_run())), encoding="utf-8")
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    code, _, err = lucy("baseline", str(report), "--out", str(blocked / "baseline.json"))
    assert code == USAGE
    assert "cannot write" in err
