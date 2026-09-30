"""The shell a scenario's `host` steps run through: bounded in time, and in what it keeps.

Every test here but one works on the pieces without starting anything. The one that does
runs a harmless command -- this interpreter printing "ok" -- through the real system shell,
because that is the only way to know the seam holds on the machine it runs on, `cmd.exe` on
Windows and `/bin/sh` everywhere else.
"""

from __future__ import annotations

import io
import subprocess
import sys
from typing import Any

import pytest

from lucy_api.evals.host import OUTPUT_LIMIT, SHOWN, Finished, failure, run_in_shell, tail


def test_a_harmless_command_runs_through_this_machine_s_shell() -> None:
    finished = run_in_shell(f'"{sys.executable}" -c "print(\'ok\')"', 60.0)
    assert finished.exit_code == 0
    assert finished.status == "ok"
    assert finished.output.strip() == "ok"


def test_a_command_still_running_at_its_timeout_is_stopped_and_what_it_printed_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: dict[str, Any] = {}

    def still_running(command: str, **options: Any) -> subprocess.CompletedProcess[bytes]:
        asked.update(options, command=command)
        options["stdout"].write(b"Stopping lucy-family-memory-1...\n")
        raise subprocess.TimeoutExpired(command, options["timeout"])

    monkeypatch.setattr(subprocess, "run", still_running)
    finished = run_in_shell("docker stop lucy-family-memory-1", 60.0)

    assert finished == Finished(exit_code=None, output="Stopping lucy-family-memory-1...\n")
    assert finished.status == "timed out"
    assert asked == {
        "command": "docker stop lucy-family-memory-1",
        "shell": True,
        "stdin": subprocess.DEVNULL,
        "stdout": asked["stdout"],
        "stderr": subprocess.STDOUT,
        "timeout": 60.0,
        "check": False,
    }


@pytest.mark.parametrize(
    ("exit_code", "status"), [(0, "ok"), (1, "exit 1"), (137, "exit 137"), (None, "timed out")]
)
def test_how_a_command_ended_is_ok_its_exit_status_or_timed_out(
    exit_code: int | None, status: str
) -> None:
    assert Finished(exit_code=exit_code, output="").status == status


def test_what_a_command_printed_is_kept_whole_when_it_is_short() -> None:
    assert tail(io.BytesIO(b"lucy-family-memory-1\n")) == "lucy-family-memory-1\n"
    assert tail(io.BytesIO(b"")) == ""
    exactly = b"x" * OUTPUT_LIMIT
    assert tail(io.BytesIO(exactly)) == "x" * OUTPUT_LIMIT


def test_only_the_end_of_a_long_output_is_kept_with_exactly_how_much_was_not() -> None:
    assert tail(io.BytesIO(b"0123456789the end"), limit=7) == (
        "[showing the last 7 of 17 bytes]\nthe end"
    )
    kept = tail(io.BytesIO(b"a" * (OUTPUT_LIMIT + 1_234)))
    assert kept == f"[showing the last 4,000 of 5,234 bytes]\n{'a' * OUTPUT_LIMIT}"


def test_bytes_that_are_not_utf8_do_not_cost_the_rest_of_the_output() -> None:
    assert tail(io.BytesIO(b"caf\xe9 ok\n")) == "caf� ok\n"


def test_a_failure_says_how_the_command_ended_and_the_last_of_what_it_printed() -> None:
    printed = "Error response from daemon:\n  No such container: lucy-family-memory-1\n"
    assert failure(Finished(exit_code=1, output=printed), 60.0) == (
        "exited 1: Error response from daemon: No such container: lucy-family-memory-1"
    )
    assert failure(Finished(exit_code=None, output=""), 60.0) == "timed out after 60s"
    assert failure(Finished(exit_code=None, output="still going\n"), 2.5) == (
        "timed out after 2.5s: still going"
    )


def test_a_long_failure_shows_only_the_end_of_what_was_printed_and_says_it_was_cut() -> None:
    said = failure(Finished(exit_code=2, output="word " * 100 + "the cause"), 60.0)
    shown = said.removeprefix("exited 2: ")
    assert len(shown) == SHOWN
    assert shown.startswith("…")
    assert shown.endswith("word the cause")
