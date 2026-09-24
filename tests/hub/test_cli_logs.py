"""`lucy logs`: the hub's log file, filtered to one conversation, read on the machine it is on."""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.cli.base import OK, USAGE
from lucy_api.cli.logs import FAMILY_LOG
from lucy_api.cli.main import main as cli_main

if TYPE_CHECKING:
    from pathlib import Path

SESSION = "ses_aaaaaaaa11112222"
OTHER = "ses_bbbbbbbb33334444"


def a_line(message: str, **fields: Any) -> dict[str, Any]:
    """One line as `JsonFormatter` writes it, with every correlation field present."""
    line: dict[str, Any] = {
        "timestamp": "2026-09-24T18:08:01.066397+00:00",
        "level": "INFO",
        "logger": "lucy_api.steps",
        "message": message,
        "request_id": None,
        "session_id": None,
        "turn_id": None,
        "agent_id": None,
        "operation": None,
        "outcome": None,
        "duration_ms": None,
    }
    return {**line, **fields}


LOG = [
    a_line("startup"),
    a_line(
        "step",
        session_id=SESSION,
        turn_id="trn_one",
        operation="help.docs",
        outcome="ok",
        duration_ms=12.5,
    ),
    a_line(
        "step",
        level="WARNING",
        session_id=SESSION,
        turn_id="trn_one",
        agent_id="agt_helper99",
        operation="broken.go",
        outcome="error",
        error_type="RuntimeError",
    ),
    a_line("turn_ended", session_id=OTHER, turn_id="trn_two", duration_ms=900.0),
]


def written(path: Path, lines: list[dict[str, Any]], *, torn: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(line) + "\n" for line in lines)
    # A bare value and a torn last line are both in a real log sooner or later; neither is a line.
    noise = '42\n{"level": "INFO", "mess' if torn else ""
    path.write_text(body + noise, encoding="utf-8")
    return path


def run(argv: list[str], tmp_path: Path, **environ: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"LUCY_CONFIG": str(tmp_path / "config.toml"), **environ}
    code = cli_main(argv, out=out, err=err, in_=io.StringIO(), environ=env)
    return code, out.getvalue(), err.getvalue()


def test_one_session_s_lines_in_short_form(tmp_path: Path) -> None:
    log = written(tmp_path / "lucy.jsonl", LOG, torn=True)

    code, out, _ = run(["logs", "--file", str(log), "--session", "aaaa1111"], tmp_path)

    assert code == OK
    shown = out.splitlines()
    assert shown == [
        "18:08:01 INFO    step session=…11112222 turn=…trn_one "
        "operation=help.docs outcome=ok 12.5ms",
        "18:08:01 WARNING step session=…11112222 turn=…trn_one agent=…helper99 "
        "operation=broken.go outcome=error error_type=RuntimeError",
    ]


@pytest.mark.parametrize(
    ("flags", "messages"),
    [
        (["--turn", "trn_two"], ["turn_ended"]),
        (["--agent", "helper99"], ["step"]),
        (["--level", "warning"], ["step"]),
        (["--grep", "startup"], ["startup"]),
        (["--last", "2"], ["step", "turn_ended"]),
    ],
)
def test_each_filter_narrows_to_what_it_names(
    tmp_path: Path, flags: list[str], messages: list[str]
) -> None:
    log = written(tmp_path / "lucy.jsonl", LOG)
    code, out, _ = run(["logs", "--file", str(log), "--json", *flags], tmp_path)
    assert code == OK
    assert [json.loads(line)["message"] for line in out.splitlines()] == messages


def test_the_family_s_own_log_is_found_without_being_named(tmp_path: Path) -> None:
    family = tmp_path / "family"
    for marker in ("family-app.json", "repos.txt"):
        written(family / marker, [])
    written(family / FAMILY_LOG, LOG)

    code, out, _ = run(["logs", "--json"], tmp_path, LUCY_FAMILY_ROOT=str(family))

    assert code == OK
    assert len(out.splitlines()) == len(LOG)


def test_no_log_file_says_where_it_looked_and_how_to_get_one(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere.jsonl"
    code, _, err = run(["logs", "--file", str(missing)], tmp_path)
    assert code == USAGE
    assert str(missing) in err
    assert "make up" in err


def test_the_family_log_is_looked_for_where_this_command_runs_without_a_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = run(["logs"], tmp_path, LUCY_FAMILY_ROOT=str(tmp_path / "not-a-family"))
    assert code == USAGE
    assert str(tmp_path / FAMILY_LOG) in err


@pytest.mark.parametrize(
    ("flags", "said"), [(["--level", "LOUD"], "unknown level"), (["--last", "0"], "--last")]
)
def test_a_bad_flag_is_a_usage_error_that_names_it(
    tmp_path: Path, flags: list[str], said: str
) -> None:
    log = written(tmp_path / "lucy.jsonl", LOG)
    code, _, err = run(["logs", "--file", str(log), *flags], tmp_path)
    assert code == USAGE
    assert said in err
