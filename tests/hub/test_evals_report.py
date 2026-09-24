"""The report: the JSON contract, the tallies, and the Markdown a person reads first.

The Markdown is held to two promises: failures come first with the evidence and the
transcript excerpt that explains them, and nothing a model wrote can break the document --
not a fence of backticks, not a pipe in a table cell, not a reply ten thousand characters
long.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lucy_api.evals.loader import parse_scenario
from lucy_api.evals.markdown import CELL_LIMIT, TEXT_LIMIT, render
from lucy_api.evals.report import (
    FORMAT,
    JSON_NAME,
    MARKDOWN_NAME,
    VERSION,
    build_report,
    flaky_checks,
    pass_rates,
    write_report,
)
from lucy_api.evals.results import (
    ERROR,
    FAILED,
    PASSED,
    SKIPPED,
    Check,
    InvocationRecord,
    ScenarioRecord,
    TurnRecord,
    outcome_for,
)
from lucy_api.evals.runner import Plan
from lucy_api.evals.transcript import Ask, ToolResult

if TYPE_CHECKING:
    from pathlib import Path

STARTED = datetime(2026, 9, 24, 10, 15, tzinfo=UTC)
ENVIRONMENT = {
    "hub": {"url": "http://127.0.0.1:8000", "version": "0.1.0", "environment": "test"},
    "client": {"version": "0.1.0", "python": "3.12.10", "platform": "win32"},
}


def scenario(name: str) -> Any:
    text = f'summary = "What {name} guards."\n[[turns]]\nsay = "Hi"\n'
    return parse_scenario(text.encode(), name=name, suite="default", path=f"default/{name}.toml")


def turn(*checks: tuple[str, bool], **overrides: Any) -> TurnRecord:
    values: dict[str, Any] = {
        "index": 1,
        "said": "Hi",
        "approve": "yes",
        "turn_id": "trn_1",
        "status": "completed",
        "termination": "success",
        "seconds": 12.0,
        "timed_out": False,
        "iterations": 2,
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_tokens": 400,
        "reply": "Hello.",
        "results": (),
        "asks": (),
        "errors": (),
        "verify": (),
        "checks": tuple(Check(name, passed, f"detail of {name}") for name, passed in checks),
    }
    values.update(overrides)
    return TurnRecord(**values)


def record(name: str, *turns: TurnRecord, **overrides: Any) -> ScenarioRecord:
    values: dict[str, Any] = {
        "scenario": f"default/{name}",
        "suite": "default",
        "name": name,
        "summary": f"What {name} guards.",
        "path": f"default/{name}.toml",
        "digest": "abc",
        "model": "clyde:haiku",
        "repeat": 1,
        "outcome": outcome_for(turns) if turns else SKIPPED,
        "session_id": "ses_1" if turns else "",
        "seconds": 12.5,
        "turns": turns,
    }
    values.update(overrides)
    return ScenarioRecord(**values)


def report(records: list[ScenarioRecord], **plan: Any) -> dict[str, Any]:
    names = sorted({item.name for item in records})
    plan.setdefault("models", ("clyde:haiku",))
    return build_report(
        plan=Plan(scenarios=tuple(scenario(name) for name in names), **plan),
        records=records,
        environment=ENVIRONMENT,
        started=STARTED,
        finished=STARTED + timedelta(seconds=75),
        selection={"suites": ["default"], "scenarios": [], "tags": []},
    )


# --------------------------------------------------------------------------------------
# The JSON document
# --------------------------------------------------------------------------------------


def test_the_json_document_carries_everything_and_says_what_it_is() -> None:
    passing = record("alpha", turn(("turn 1: status is completed", True)))
    failing = record("beta", turn(("turn 1: status is completed", False), seconds=30.0))
    document = report([passing, failing])

    assert document["format"] == FORMAT
    assert document["version"] == VERSION
    assert document["started_at"] == "2026-09-24T10:15:00Z"
    assert document["finished_at"] == "2026-09-24T10:16:15Z"
    assert document["seconds"] == 75.0
    assert document["passed"] is False
    assert document["stopped"] == ""
    assert document["comparison"] is None
    assert document["plan"] == {
        "models": ["clyde:haiku"],
        "profile": "personal",
        "repeat": 1,
        "timeout_seconds": 300.0,
        "keep_sessions": False,
        "scenarios": ["default/alpha", "default/beta"],
        "prompts": 2,
        "selection": {"suites": ["default"], "scenarios": [], "tags": []},
    }
    first = document["runs"][0]
    assert first["checks_passed"] == first["checks_total"] == 1
    assert first["turns"][0]["checks"][0] == {
        "name": "turn 1: status is completed",
        "passed": True,
        "detail": "detail of turn 1: status is completed",
    }
    assert document["summary"]["clyde:haiku"] == {
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "error": 0,
        "runs": 2,
        "checks_passed": 1,
        "checks_total": 2,
        "turns": 2,
        "median_turn_seconds": 21.0,
        "input_tokens": 2000,
        "output_tokens": 100,
        "cache_read_tokens": 800,
    }
    json.dumps(document)


def test_a_run_with_nothing_failing_passes_unless_it_stopped() -> None:
    clean = report([record("alpha", turn(("turn 1: x", True)))])
    assert clean["passed"] is True
    stopped = build_report(
        plan=Plan(scenarios=(scenario("alpha"),), models=("clyde:haiku",)),
        records=[],
        environment=ENVIRONMENT,
        started=STARTED,
        finished=STARTED,
        stopped="interrupted with Ctrl-C",
    )
    assert stopped["passed"] is False
    assert stopped["plan"]["selection"] == {}
    assert stopped["summary"]["clyde:haiku"]["median_turn_seconds"] is None


def test_pass_rates_leave_skipped_runs_out_and_find_the_flaky_checks() -> None:
    runs = [
        record("alpha", turn(("a", True), ("b", True)), repeat=1),
        record("alpha", turn(("a", True), ("b", False)), repeat=2),
        record("alpha", turn(("a", True), ("b", False)), repeat=3),
        record("gamma", outcome=SKIPPED, reason="research is not ready"),
    ]
    document = report(runs, repeat=3)
    assert document["pass_rates"] == {
        "clyde:haiku": {
            "default/alpha": {
                "runs": 3,
                "passed": 1,
                "checks": {"a": {"runs": 3, "passed": 3}, "b": {"runs": 3, "passed": 1}},
            }
        }
    }
    assert flaky_checks(document) == [("clyde:haiku", "default/alpha", "b", 1, 3)]
    assert pass_rates([]) == {}


def test_both_files_are_written_where_asked(tmp_path: Path) -> None:
    document = report([record("alpha", turn(("a", True)))])
    as_json, as_markdown = write_report(tmp_path, document, markdown="# hi\n")
    assert as_json == tmp_path / JSON_NAME
    assert as_markdown == tmp_path / MARKDOWN_NAME
    assert json.loads(as_json.read_text(encoding="utf-8")) == json.loads(json.dumps(document))
    assert as_markdown.read_text(encoding="utf-8") == "# hi\n"


# --------------------------------------------------------------------------------------
# The Markdown
# --------------------------------------------------------------------------------------


def test_a_clean_run_says_so_and_lists_every_run() -> None:
    text = render(report([record("alpha", turn(("a", True)))]))
    assert text.startswith("# Lucy eval, 2026-09-24T10:15:00Z\n\n**Every check passed.**\n")
    assert "| Hub | http://127.0.0.1:8000 (version 0.1.0) |" in text
    assert "| Took | 1m 15s |" in text
    assert "| Client | lucy 0.1.0 |" in text
    assert "| clyde:haiku | 1 | 0 | 0 | 0 | 1/1 | 12.0s | 1,000 / 50 / 400 |" in text
    assert "| default/alpha | clyde:haiku | 1 | pass | 1/1 | 12.5s | ses_1 |" in text
    for absent in ("## Failures", "## By scenario", "## Flaky", "## Compared", "## Skipped"):
        assert absent not in text
    assert text.endswith("|\n")


def test_a_run_where_everything_was_skipped_says_nothing_ran() -> None:
    document = report([record("alpha", outcome=SKIPPED, reason="research | not ready")])
    document["environment"] = {}
    text = render(document)
    assert "**Nothing ran:** every scenario was skipped. 1 skipped." in text
    assert "| Hub | (version unknown) |" in text
    assert "## Skipped" in text
    assert "| default/alpha | clyde:haiku | research \\| not ready |" in text
    assert "| clyde:haiku | 0 | 0 | 0 | 1 | 0/0 | - | 0 / 0 / 0 |" in text
    assert "| default/alpha | clyde:haiku | 1 | skip | 0/0 | 12.5s | - |" in text


def test_a_run_that_stopped_leads_with_why() -> None:
    document = report([record("alpha", outcome=SKIPPED)])
    document["stopped"] = "cannot reach Lucy at http://127.0.0.1:8000"
    text = render(document)
    assert "**The run stopped early:** cannot reach Lucy at http://127.0.0.1:8000\n" in text
    assert "skipped." not in text.split("\n")[2]


def test_failures_come_first_with_the_evidence_and_the_turn_that_failed() -> None:
    reply = "Done. ```json\n{}\n```\n" + "x" * (TEXT_LIMIT + 5)
    failing = turn(
        ("turn 2: reply avoids /fence/", False),
        ("turn 2: status is completed", True),
        index=2,
        status="",
        reply=reply,
        results=(
            ToolResult("workspace.write", "ok", note="write the page | with a pipe"),
            ToolResult("notes.setFact", "error", error="e" * (CELL_LIMIT + 1)),
        ),
        asks=(
            Ask("apr_1", "notes.setFact", arguments="title=Drink", answer="approved"),
            Ask("apr_2", "workspace.delete"),
        ),
        errors=("empty_reply: said nothing",),
        verify=(
            InvocationRecord("workspace.read", {"path": "a"}, "error", error="no file a"),
            InvocationRecord("workspace.read", {"path": "b"}, "ok", output="print"),
        ),
    )
    passing = turn(("turn 1: status is completed", True), reply="")
    broken = record(
        "beta",
        passing,
        failing,
        reason="could not archive ses_1: gone",
        seed=(InvocationRecord("workspace.write", {"path": "notes.md"}, "ok", output="{}"),),
    )
    errored = record(
        "gamma", outcome=ERROR, reason="stopped before a session existed: 503", session_id=""
    )
    text = render(report([record("alpha", turn(("a", True))), broken, errored]))

    assert "**2 of 3 runs did not pass.**" in text
    assert text.index("## Failures") < text.index("## Every run")
    assert "### default/beta with clyde:haiku, run 1\n\nWhat beta guards.\n" in text
    assert "**failed** after 12.5s, session `ses_1`." in text
    assert "Why: could not archive ses_1: gone" in text
    assert "- `turn 2: reply avoids /fence/`: detail of turn 2: reply avoids /fence/" in text
    assert "Seeded:" in text
    assert "#### Turn 2: unknown in 12.0s, 2 round(s), 2 step(s)" in text
    assert "#### Turn 1:" not in text
    assert "````text\nDone. ```json" in text
    assert f"[showing {TEXT_LIMIT:,} of {len(reply):,} characters; report.json has all]" in text
    assert "| workspace.write | ok | write the page \\| with a pipe |" in text
    assert f"[showing {CELL_LIMIT} of {CELL_LIMIT + 1} characters" in text
    assert "- Asked to approve notes.setFact (approved): title=Drink" in text
    assert "- Asked to approve workspace.delete (unanswered)" in text
    assert "- The transcript recorded empty_reply: said nothing" in text
    assert "| workspace.read | error | no file a |" in text
    assert "| workspace.read | ok | print |" in text
    assert "### default/gamma with clyde:haiku, run 1" in text
    assert "**error** after 12.5s." in text


def test_a_turn_with_no_reply_says_so() -> None:
    silent = turn(("turn 1: reply is not empty", False), reply="")
    text = render(report([record("alpha", silent)]))
    assert "Lucy:\n\n*(no reply)*" in text


def test_several_models_or_runs_get_a_scenario_by_model_table() -> None:
    runs = [
        record("alpha", turn(("a", True)), model="clyde:haiku"),
        record("alpha", turn(("a", False)), model="clyde:sonnet"),
        record("beta", turn(("b", True)), model="clyde:haiku"),
        record("beta", turn(("b", False)), model="clyde:haiku", repeat=2),
        record("beta", outcome=SKIPPED, model="clyde:haiku", repeat=3),
    ]
    document = report(runs, models=("clyde:haiku", "clyde:sonnet"))
    text = render(document)
    assert "| Scenario | clyde:haiku | clyde:sonnet |" in text
    assert "| default/alpha | pass | FAIL |" in text
    assert "| default/beta | 1/2 | - |" in text


def test_flaky_checks_are_listed_with_how_often_they_held() -> None:
    runs = [
        record("alpha", turn(("a", True)), repeat=1),
        record("alpha", turn(("a", False)), repeat=2),
    ]
    text = render(report(runs, repeat=2))
    assert "## Flaky checks" in text
    assert "| clyde:haiku | default/alpha | a | 1/2 |" in text


def test_a_comparison_is_rendered_in_full() -> None:
    document = report([record("alpha", turn(("a", True)))])
    row = {"model": "clyde:haiku", "scenario": "default/alpha"}
    document["comparison"] = {
        "previous": "var/evals/old/report.json",
        "regressions": [{**row, "before": "passed", "after": "failed"}],
        "fixes": [{**row, "before": "failed", "after": "passed"}],
        "moved": [],
        "skipped": [{**row, "before": "passed", "after": "skipped"}],
        "new": [row],
        "removed": [],
        "changed": [row],
        "checks": [{**row, "check": "a", "before": "1/1", "after": "0/1"}],
    }
    text = render(document)
    assert "## Compared with var/evals/old/report.json\n\n**1 regression(s).**" in text
    assert "Regressions:\n\n- default/alpha with clyde:haiku: passed -> failed" in text
    assert "Fixes:" in text
    assert "Pass rate moved:" not in text
    assert "Skipped in one run and not the other:" in text
    assert "New scenarios:\n\n- default/alpha with clyde:haiku" in text
    assert "Removed scenarios:" not in text
    assert "Scenario files that changed:" in text
    assert "| clyde:haiku | default/alpha | a | 1/1 | 0/1 |" in text


def test_a_comparison_with_nothing_to_say_says_no_regressions() -> None:
    document = report([record("alpha", turn(("a", True)))])
    document["comparison"] = {
        "previous": "old",
        **{
            key: []
            for key in ("regressions", "fixes", "moved", "skipped", "new", "removed", "changed")
        },
        "checks": [],
    }
    assert "## Compared with old\n\nNo regressions.\n" in render(document)


def test_outcomes_are_labelled_for_people() -> None:
    assert outcome_for(()) == PASSED
    assert outcome_for((turn(("a", False)),)) == FAILED
