"""`lucy eval`: listing, planning, holding the run, and every way it refuses to start.

The command is driven through `lucy`'s own `main`, with `httpx.Client` swapped for one on
`FakeLucy`, so the flags, the output streams and the exit codes are the ones a person gets.
Nothing sleeps and nothing leaves `tmp_path`.
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
from eval_fakes import Clock, FakeLucy, Play

from lucy_api.cli import evals as command
from lucy_api.cli.base import OK, REFUSED, TOKEN_VAR, UNREACHABLE, USAGE
from lucy_api.cli.main import main as cli_main
from lucy_api.evals.conversation import Pace
from lucy_api.evals.report import JSON_NAME, MARKDOWN_NAME

INTERRUPTED = 130
WORD = "eval"

SUITE = {
    "greeting.toml": 'summary = "Says hello. Nothing more."\ntags = ["smoke"]\n'
    '[[turns]]\nsay = "Hello?"\n[turns.expect]\nreply_matches = ["hello"]\n',
    "tea.toml": 'summary = "Remembers tea."\ntags = ["memory"]\n'
    '[[turns]]\nsay = "Remember tea."\n[turns.expect]\nran = ["notes.setFact"]\n',
}


REAL_UTC_NOW = command.utc_now
REAL_PACE = command.pace


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    clock = Clock()
    monkeypatch.setattr(command, "pace", lambda: Pace(clock=clock, sleep=clock.sleep))
    monkeypatch.setattr(command, "utc_now", lambda: datetime(2026, 9, 24, 10, 15, tzinfo=UTC))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLucy:
    hub = FakeLucy()
    hub.say("Hello?", Play(reply="Hello there."))
    hub.say("Remember tea.", Play(reply="Saved.", ran=(("notes.setFact", "ok"),)))
    monkeypatch.setattr(httpx, "Client", lambda **_: hub.client())
    return hub


@pytest.fixture
def suite(tmp_path: Path) -> Path:
    folder = tmp_path / "mine"
    folder.mkdir()
    for name, text in SUITE.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def lucy(*argv: str, token: str = "t", url: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environ = {"LUCY_CONFIG": str(Path.cwd() / "config.toml")}
    if token:
        environ[TOKEN_VAR] = token
    if url:
        environ["LUCY_URL"] = url
    code = cli_main([WORD, *argv], out=out, err=err, in_=io.StringIO(), environ=environ)
    return code, out.getvalue(), err.getvalue()


def run(*argv: str, **kwargs: Any) -> tuple[int, str, str]:
    return lucy("run", "--model", "clyde:haiku", *argv, **kwargs)


def reports(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "var" / "evals").iterdir())


# --------------------------------------------------------------------------------------
# lucy eval, lucy eval list
# --------------------------------------------------------------------------------------


def test_the_bare_command_prints_its_help() -> None:
    code, out, _ = lucy()
    assert code == OK
    assert out.startswith(f"usage: lucy {WORD} ")
    assert "docs: docs/evals.md" in out


def test_list_shows_the_shipped_suite_by_default() -> None:
    code, out, _ = lucy("list")
    assert code == OK
    assert "default: 7 scenario(s), 10 prompt(s)  (shipped)" in out
    assert "  planted-instructions     1 turn(s)  [security, workspace, trust-boundary]" in out
    assert "A planted file's instructions are data, not orders." in out


def test_list_reads_a_folder_and_speaks_json(suite: Path) -> None:
    code, out, _ = lucy("list", "--suite", str(suite), "--json")
    assert code == OK
    payload = json.loads(out)
    listed = payload["suites"][0]
    assert listed["name"] == "mine"
    assert [scenario["qualified"] for scenario in listed["scenarios"]] == [
        "mine/greeting",
        "mine/tea",
    ]
    assert listed["scenarios"][0] == {
        "name": "greeting",
        "qualified": "mine/greeting",
        "summary": "Says hello. Nothing more.",
        "tags": ["smoke"],
        "turns": 1,
        "requires": [],
        "path": str(suite / "greeting.toml"),
    }
    code, out, _ = lucy("list", "--suite", str(suite))
    assert "Says hello." in out
    assert "Nothing more" not in out


def test_a_bad_scenario_file_is_a_usage_error_that_names_the_key(tmp_path: Path) -> None:
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "typo.toml").write_text(
        'summary = "x"\n[[turns]]\nsay = "hi"\n[turns.expect]\nreply_match = ["x"]\n',
        encoding="utf-8",
    )
    code, out, err = lucy("list", "--suite", str(folder))
    assert code == USAGE
    assert out == ""
    assert "turns[1].expect.reply_match: unknown key; did you mean `reply_matches`?" in err
    assert "docs/evals.md lists every key" in err


def test_the_same_suite_twice_is_refused() -> None:
    code, _, err = lucy("list", "--suite", "default", "--suite", "default")
    assert code == USAGE
    assert "two suites are called default" in err


# --------------------------------------------------------------------------------------
# Refusing to start
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["run"],
        ["run", "--model", "clyde:haiku", "--repeat", "0"],
        ["run", "--model", "clyde:haiku", "--repeat", "two"],
        ["run", "--model", "clyde:haiku", "--timeout", "0"],
        ["run", "--model", "clyde:haiku", "--timeout", "soon"],
        ["run", "--model", "clyde:haiku", "--timeout", "inf"],
    ],
)
def test_a_malformed_command_is_refused_by_the_parser(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        lucy(*argv)
    assert caught.value.code == USAGE


@pytest.mark.parametrize(
    ("models", "complaint"),
    [
        (["haiku"], "'haiku' is not a model spec"),
        ([" :haiku"], "is not a model spec"),
        (["clyde:"], "is not a model spec"),
        (["clyde:haiku", "clyde:haiku "], "clyde:haiku is named twice"),
    ],
)
def test_a_model_spec_must_be_provider_colon_model_and_named_once(
    fake: FakeLucy, models: list[str], complaint: str
) -> None:
    argv = [part for model in models for part in ("--model", model)]
    code, _, err = lucy("run", *argv)
    assert code == USAGE
    assert complaint in err
    assert fake.requests == []


def test_a_scenario_that_does_not_exist_is_named(fake: FakeLucy) -> None:
    code, _, err = run("--scenario", "nope")
    assert code == USAGE
    assert "no scenario called 'nope'" in err
    assert "`lucy eval list` shows every scenario" in err


def test_a_name_in_two_suites_must_say_which(fake: FakeLucy, suite: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "greeting.toml").write_text(SUITE["greeting.toml"], encoding="utf-8")
    code, _, err = run("--suite", str(suite), "--suite", str(other), "--scenario", "greeting")
    assert code == USAGE
    assert "'greeting' is in more than one suite: mine/greeting, other/greeting" in err
    code, _, _ = run(
        "--suite", str(suite), "--suite", str(other), "--scenario", "other/greeting", "--dry-run"
    )
    assert code == OK


def test_tags_narrow_the_selection_and_an_empty_selection_is_refused(
    fake: FakeLucy, suite: Path
) -> None:
    code, out, _ = run("--suite", str(suite), "--tag", "memory", "--dry-run", "--json")
    assert code == OK
    assert [row["scenario"] for row in json.loads(out)["scenarios"]] == ["mine/tea"]
    code, _, err = run("--suite", str(suite), "--tag", "nothing-has-this")
    assert code == USAGE
    assert "no scenario matches that selection" in err


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000",
        "http://127.0.0.2:8000",
        "http://[::1]:8000",
        "http://lucy.localhost:8000",
    ],
)
def test_this_machine_is_allowed_however_it_is_spelled(fake: FakeLucy, url: str) -> None:
    code, _, _ = run("--dry-run", url=url)
    assert code == OK


@pytest.mark.parametrize("url", ["http://box:8000", "http://10.0.0.5:8000"])
def test_a_hub_on_another_machine_needs_to_be_allowed(fake: FakeLucy, url: str) -> None:
    code, _, err = run("--dry-run", url=url)
    assert code == USAGE
    assert f"{url} is not this machine" in err
    assert "--allow-remote" in err
    assert fake.requests == []
    assert run("--dry-run", "--allow-remote", url=url)[0] == OK


def test_without_a_token_nothing_is_sent(fake: FakeLucy) -> None:
    code, _, err = run(token="")
    assert code == USAGE
    assert "not signed in" in err
    assert fake.requests == []


def test_a_previous_report_that_cannot_be_read_is_refused_before_the_run(
    fake: FakeLucy, tmp_path: Path
) -> None:
    code, _, err = run("--compare", str(tmp_path / "missing.json"))
    assert code == USAGE
    assert "cannot read" in err
    assert "--compare takes a report.json or its folder" in err
    assert fake.requests == []


# --------------------------------------------------------------------------------------
# Before anything is created
# --------------------------------------------------------------------------------------


def test_a_hub_that_does_not_answer_is_unreachable(fake: FakeLucy) -> None:
    fake.fail[("GET", "/healthy")] = httpx.ConnectError("refused")
    code, _, err = run()
    assert code == UNREACHABLE
    assert "cannot reach Lucy at http://127.0.0.1:8000" in err
    assert "(ConnectError)" in err


def test_a_refused_token_is_a_usage_error(fake: FakeLucy) -> None:
    fake.fail[("GET", "/v1/models")] = 401
    code, _, err = run()
    assert code == USAGE
    assert "the hub refused the token" in err


def test_a_hub_that_cannot_answer_the_preflight_says_so(fake: FakeLucy) -> None:
    fake.fail[("GET", "/healthy")] = 500
    code, _, err = run()
    assert code == USAGE
    assert "the hub could not answer before the run: GET /healthy answered 500" in err
    assert "lucy doctor" in err


@pytest.mark.parametrize(
    ("model", "complaint"),
    [
        ("mistral:large", "mistral:large: this hub has no model provider called 'mistral'"),
        ("openai:gpt-5", "openai:gpt-5 is not usable on this hub: openai is unavailable: no key"),
        ("groq:llama", "groq:llama is not usable on this hub: groq is unavailable\n"),
    ],
)
def test_a_model_the_hub_cannot_use_is_refused_naming_what_it_can(
    fake: FakeLucy, model: str, complaint: str
) -> None:
    code, _, err = lucy("run", "--model", model)
    assert code == USAGE
    assert complaint in err
    assert "usable on this hub: clyde:haiku, clyde:sonnet, anthropic:<model>" in err
    assert fake.sessions == {}


def test_a_hub_with_nothing_usable_says_so(fake: FakeLucy) -> None:
    fake.models = {"ready": [], "available": [], "unavailable": ["not a row"]}
    code, _, err = run()
    assert code == USAGE
    assert "nothing is usable; run `lucy models`" in err


def test_a_local_model_the_runtime_does_not_list_is_a_warning_not_a_refusal(
    fake: FakeLucy, suite: Path
) -> None:
    code, _, err = lucy(
        "run",
        "--model",
        "clyde:opus",
        "--model",
        "anthropic:claude",
        "--suite",
        str(suite),
        "--dry-run",
    )
    assert code == OK
    assert "warning: clyde:opus: clyde lists haiku, sonnet, not opus" in err
    assert "anthropic" not in err.split("warning")[-1].split("\n", 1)[1]


def test_a_dry_run_prints_the_plan_and_creates_nothing(fake: FakeLucy) -> None:
    fake.capabilities = [
        {"id": "research", "usable": False, "state": "not_connected", "detail": "sign in"}
    ]
    code, out, err = run("--dry-run", "--repeat", "2")
    assert code == OK
    assert err == ""
    assert out.startswith(
        "Would hold 7 scenario(s) x 1 model(s) x 2 run(s) = 14 conversation(s), 20 prompt(s), "
        "with clyde:haiku on http://127.0.0.1:8000 (hub 0.1.0) as profile personal."
    )
    assert (
        "default/research-with-source     1 turn(s)  would skip: research is not_connected: sign in"
        in out
    )
    assert out.rstrip().endswith("Nothing was created: this was a dry run.")
    assert fake.sessions == {}
    assert all(request.method == "GET" for request in fake.requests)


def test_a_dry_run_speaks_json(fake: FakeLucy) -> None:
    fake.version = ""
    code, out, _ = run("--dry-run", "--json", "--profile", "work", "--scenario", "what-can-you-do")
    assert code == OK
    assert json.loads(out) == {
        "dry_run": True,
        "hub": {"url": "http://127.0.0.1:8000", "version": "", "environment": "test"},
        "models": ["clyde:haiku"],
        "profile": "work",
        "repeat": 1,
        "conversations": 1,
        "prompts": 1,
        "scenarios": [
            {
                "scenario": "default/what-can-you-do",
                "turns": 1,
                "seeds": 0,
                "requires": [],
                "skip": None,
            }
        ],
    }
    assert not any(request.url.path == "/v1/capabilities" for request in fake.requests)


# --------------------------------------------------------------------------------------
# Holding the run
# --------------------------------------------------------------------------------------


def test_a_passing_run_writes_its_report_and_exits_zero(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    code, out, err = run("--suite", str(suite))
    assert code == OK
    [folder] = reports(tmp_path)
    assert folder.name == "20260924T101500Z"
    assert (folder / JSON_NAME).is_file()
    assert (folder / MARKDOWN_NAME).is_file()
    assert out.splitlines() == [
        "clyde:haiku: 2 passed, 0 failed, 0 error(s), 0 skipped; 14/14 checks held",
        f"report: {Path('var', 'evals', '20260924T101500Z', MARKDOWN_NAME)}",
    ]
    assert "Holding 2 scenario(s) x 1 model(s) x 1 run(s)" in err
    assert "[1/2] mine/greeting with clyde:haiku" in err
    assert "    turn 1: completed in 1.0s, 1 round(s), 0 step(s)  ok" in err
    assert "    pass" in err
    saved = json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["passed"] is True
    assert saved["environment"]["hub"] == {
        "url": "http://127.0.0.1:8000",
        "version": "0.1.0",
        "environment": "test",
    }
    assert saved["environment"]["client"]["version"] == "0.1.0"
    assert saved["plan"]["selection"] == {"suites": [str(suite)], "scenarios": [], "tags": []}
    assert len(fake.archived) == 2


def test_a_failing_run_exits_one_and_names_what_failed(fake: FakeLucy, suite: Path) -> None:
    fake.say("Hello?", Play(reply="<invoke name='x'> ```json"))
    code, out, err = run("--suite", str(suite), "--repeat", "2")
    assert code == REFUSED
    assert (
        "  FAIL mine/greeting with clyde:haiku, run 1: turn 1: reply matches /hello/ (and 2 more)"
        in out
    )
    assert "[1/4] mine/greeting with clyde:haiku, run 1" in err
    assert "  FAIL" in err


def test_an_error_without_a_failing_check_is_explained_by_its_reason(
    fake: FakeLucy, suite: Path
) -> None:
    fake.fail[("POST", "/v1/sessions")] = 503
    code, out, err = run("--suite", str(suite), "--scenario", "tea", "--json")
    assert code == REFUSED
    payload = json.loads(out)
    assert payload["passed"] is False
    assert payload["failures"] == [
        {
            "scenario": "mine/tea",
            "model": "clyde:haiku",
            "repeat": 1,
            "outcome": "error",
            "reason": "stopped before a session existed: POST /v1/sessions answered 503: "
            "/v1/sessions is failing on purpose",
            "failing_checks": [],
        }
    ]
    assert payload["report"]["markdown"].endswith(MARKDOWN_NAME)
    assert payload["comparison"] is None
    assert err == ""
    code, out, _ = run("--suite", str(suite), "--scenario", "tea")
    assert "ERROR mine/tea with clyde:haiku, run 1: stopped before a session existed" in out


def test_a_run_compared_with_the_last_one_lists_its_regressions(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    assert run("--suite", str(suite))[0] == OK
    [previous] = reports(tmp_path)
    fake.say("Hello?", Play(reply="Goodbye."))
    code, out, _ = run("--suite", str(suite), "--compare", str(previous))
    assert code == REFUSED
    assert f"compared with {previous}: 1 regression(s), 0 fix(es)" in out
    assert "  regressed: mine/greeting with clyde:haiku (passed -> failed)" in out
    [_, latest] = reports(tmp_path)
    assert latest.name == "20260924T101500Z-2"
    saved = json.loads((latest / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["comparison"]["regressions"][0]["scenario"] == "mine/greeting"


def test_a_report_folder_is_never_written_over(fake: FakeLucy, suite: Path, tmp_path: Path) -> None:
    target = tmp_path / "chosen" / "deep"
    assert run("--suite", str(suite), "--report-dir", str(target))[0] == OK
    assert (target / JSON_NAME).is_file()
    code, _, err = run("--suite", str(suite), "--report-dir", str(target))
    assert code == USAGE
    assert "already holds a report" in err


def test_a_report_folder_that_cannot_be_made_is_refused(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("", encoding="utf-8")
    code, _, err = run("--suite", str(suite), "--report-dir", str(blocker / "under"))
    assert code == USAGE
    assert "cannot create" in err
    assert fake.sessions == {}


def test_a_hub_that_goes_away_mid_run_still_leaves_a_report(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    fake.fail[("GET", "/v1/turns/trn_1")] = httpx.ConnectError("gone")
    code, _, err = run("--suite", str(suite))
    assert code == UNREACHABLE
    assert "cannot reach Lucy" in err
    [folder] = reports(tmp_path)
    saved = json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["stopped"] == "cannot reach Lucy at http://127.0.0.1:8000"
    assert [run["outcome"] for run in saved["runs"]] == ["error", "error"]


def test_a_token_refused_mid_run_stops_it_as_a_usage_error(fake: FakeLucy, suite: Path) -> None:
    fake.fail[("GET", "/v1/turns/trn_1")] = 401
    code, _, err = run("--suite", str(suite))
    assert code == USAGE
    assert "the run stopped early: GET /v1/turns/trn_1 answered 401" in err


def test_ctrl_c_writes_the_report_so_far_and_exits_130(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    fake.fail[("GET", "/v1/turns/trn_2")] = KeyboardInterrupt()
    code, _, _ = run("--suite", str(suite))
    assert code == INTERRUPTED
    [folder] = reports(tmp_path)
    saved = json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["stopped"] == "interrupted with Ctrl-C"
    assert [run["scenario"] for run in saved["runs"]] == ["mine/greeting"]
    assert fake.archived == ["ses_1", "ses_2"]


def test_the_plan_names_an_unknown_hub_version(fake: FakeLucy, suite: Path) -> None:
    fake.version = ""
    _, _, err = run("--suite", str(suite), "--scenario", "greeting")
    assert "(hub unknown)" in err


def test_a_chosen_timeout_is_recorded_in_the_plan(
    fake: FakeLucy, suite: Path, tmp_path: Path
) -> None:
    assert run("--suite", str(suite), "--scenario", "greeting", "--timeout", "45")[0] == OK
    [folder] = reports(tmp_path)
    saved = json.loads((folder / JSON_NAME).read_text(encoding="utf-8"))
    assert saved["plan"]["timeout_seconds"] == 45.0


def test_the_real_seams_use_the_wall_clock() -> None:
    now = REAL_UTC_NOW()
    assert now.tzinfo is UTC
    waits = REAL_PACE()
    assert waits.poll_seconds == 1.0
    assert waits.clock() <= waits.clock()
