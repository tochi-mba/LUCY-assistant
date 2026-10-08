"""One turn against the fake CLI: flags, counters, transcript, and every honest ending."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import a_runner, fake_command

from lucy_coder.runner import (
    NOT_INSTALLED,
    SIGNED_OUT,
    ClaudeRunner,
    Counters,
    doctor,
)

if TYPE_CHECKING:
    from lucy_coder.runner import TurnOutcome


async def one_turn(
    runner: ClaudeRunner,
    cwd: str,
    *,
    script: str = "answers",
    resume: bool = False,
    run_level: str = "edits",
) -> tuple[TurnOutcome, Counters, list[str]]:
    os.environ["FAKE_CLAUDE"] = script
    counters = Counters()
    lines: list[str] = []
    try:
        outcome = await runner.run_turn(
            task_id="tsk_1",
            prompt="write hello.txt saying hi",
            cwd=cwd,
            session_id="ses-fake-1",
            resume=resume,
            run_level=run_level,
            counters=counters,
            transcribe=lines.append,
        )
    finally:
        del os.environ["FAKE_CLAUDE"]
    return outcome, counters, lines


async def test_a_turn_streams_counts_and_ends_with_the_answer(workdir: str) -> None:
    outcome, counters, lines = await one_turn(a_runner(), workdir)

    assert outcome.ok
    assert outcome.result == "done: wrote hello.txt"
    assert outcome.cost_usd == pytest.approx(0.021)
    assert outcome.num_turns == 1
    assert counters.tool_uses == 1
    assert counters.last_tool == "Write"
    assert counters.last_text == "writing the file now"
    assert len(lines) == 4, "every stream line reached the transcript"


async def test_the_cli_is_invoked_with_the_contract_flags_in_the_tasks_directory(
    workdir: str,
) -> None:
    await one_turn(a_runner(budget=0.75), workdir, run_level="full")

    seen = json.loads((Path(workdir) / "argv.json").read_text(encoding="utf-8"))
    argv = seen["argv"]
    assert seen["cwd"].lower() == workdir.lower(), "the task runs in its own directory"
    assert argv[:2] == ["-p", "write hello.txt saying hi"]
    assert tuple(argv[argv.index("--output-format") : argv.index("--output-format") + 2]) == (
        "--output-format",
        "stream-json",
    )
    assert "--verbose" in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "0.75"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--session-id") + 1] == "ses-fake-1"
    assert "--resume" not in argv


async def test_a_follow_up_resumes_the_session_rather_than_starting_one(workdir: str) -> None:
    await one_turn(a_runner(), workdir, resume=True)

    argv = json.loads((Path(workdir) / "argv.json").read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--resume") + 1] == "ses-fake-1"
    assert "--session-id" not in argv


async def test_run_levels_map_to_permission_modes_and_unknown_falls_to_edits(
    workdir: str,
) -> None:
    for asked, mode in (("plan", "plan"), ("edits", "acceptEdits"), ("made-up", "acceptEdits")):
        await one_turn(a_runner(), workdir, run_level=asked)
        argv = json.loads((Path(workdir) / "argv.json").read_text(encoding="utf-8"))["argv"]
        assert argv[argv.index("--permission-mode") + 1] == mode, asked


async def test_a_spent_budget_is_a_failure_that_names_the_cap(workdir: str) -> None:
    outcome, _counters, _lines = await one_turn(a_runner(budget=0.5), workdir, script="budget")

    assert not outcome.ok
    assert "$0.50 budget" in outcome.detail
    assert outcome.cost_usd == pytest.approx(0.22), "what it spent is still accounted"


async def test_a_session_limit_fails_with_the_clis_own_sentence(workdir: str) -> None:
    outcome, _counters, _lines = await one_turn(a_runner(), workdir, script="limit")

    assert not outcome.ok
    assert "hit your session limit" in outcome.detail


async def test_a_signed_out_cli_is_named_rather_than_a_mystery_exit(workdir: str) -> None:
    outcome, _counters, _lines = await one_turn(a_runner(), workdir, script="signed-out")

    assert not outcome.ok
    assert outcome.detail == SIGNED_OUT


async def test_a_missing_binary_says_claude_is_not_installed(workdir: str) -> None:
    gone = ClaudeRunner(["claude-nowhere-to-be-found"], budget_usd=0.5, timeout_seconds=5)
    counters = Counters()
    outcome = await gone.run_turn(
        task_id="tsk_1",
        prompt="anything",
        cwd=workdir,
        session_id="ses-1",
        resume=False,
        run_level="edits",
        counters=counters,
        transcribe=lambda _line: None,
    )

    assert not outcome.ok
    assert outcome.detail == NOT_INSTALLED


async def test_a_turn_past_the_wall_clock_is_killed_and_says_so(workdir: str) -> None:
    outcome, _counters, _lines = await one_turn(a_runner(timeout=3.0), workdir, script="hangs")

    assert not outcome.ok
    assert "stopped" in outcome.detail
    assert "0-minute" in outcome.detail


async def test_a_non_json_line_is_transcribed_and_never_crashes_the_turn(workdir: str) -> None:
    outcome, _counters, lines = await one_turn(a_runner(), workdir, script="garbled")

    assert outcome.ok
    assert any(line.startswith("warning:") for line in lines)


async def test_cancel_kills_a_live_turn_and_a_dead_one_is_a_no(workdir: str) -> None:
    runner = a_runner(timeout=60.0)
    os.environ["FAKE_CLAUDE"] = "hangs"
    try:
        import asyncio

        counters = Counters()
        turn = asyncio.ensure_future(
            runner.run_turn(
                task_id="tsk_hang",
                prompt="anything",
                cwd=workdir,
                session_id="ses-1",
                resume=False,
                run_level="edits",
                counters=counters,
                transcribe=lambda _line: None,
            )
        )
        for _ in range(200):
            if counters.events:
                break
            await asyncio.sleep(0.05)
        assert counters.events, "the fake started and spoke"
        assert await runner.cancel("tsk_hang") is True
        outcome = await turn
        assert not outcome.ok
    finally:
        del os.environ["FAKE_CLAUDE"]
    assert await runner.cancel("tsk_hang") is False, "nothing live is nothing to kill"


def test_doctor_names_a_missing_cli_and_blesses_a_working_one() -> None:
    assert doctor(["claude-nowhere-to-be-found"]) == NOT_INSTALLED
    assert doctor([*fake_command(), "--version"][:2]) == ""


def test_doctor_names_a_broken_and_a_silent_cli() -> None:
    import sys as _sys

    broken = doctor([_sys.executable, "-c", "import sys; sys.exit(7)"])
    assert broken == "`claude --version` failed (exit 7)"
    silent = doctor([_sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.5)
    assert "did not answer within 0 seconds" in silent or "did not answer" in silent


async def test_killing_a_process_that_already_ended_is_nothing(workdir: str) -> None:
    import asyncio as _asyncio

    done = await _asyncio.create_subprocess_exec(
        *fake_command()[:1], "-c", "print('bye')", stdout=_asyncio.subprocess.DEVNULL
    )
    await done.wait()
    runner = a_runner()
    await runner._kill(done)
    assert done.returncode is not None


def test_a_native_program_is_used_as_found(tmp_path: Path) -> None:
    from lucy_coder.runner import resolve_command

    program = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    program.write_text("", encoding="utf-8")
    program.chmod(0o755)
    os.environ["PATH"] = str(tmp_path) + os.pathsep + os.environ["PATH"]
    try:
        found, flag = resolve_command(["claude", "--x"])
        assert found.casefold() == str(program).casefold(), "PATHEXT may change the case"
        assert flag == "--x"
    finally:
        os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]


def test_an_npm_shim_is_followed_to_the_binary_it_forwards_to(tmp_path: Path) -> None:
    """The bug, named: on this machine `claude` is npm's claude.cmd. A bare name could not
    be launched without a shell, and through cmd.exe a brief the model wrote would meet
    `&` and `|` as syntax. The shim's own binary is run instead, with no shell between."""
    from lucy_coder.runner import NPM_NATIVE, SHIMS, resolve_command

    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    native = tmp_path / NPM_NATIVE / "claude.exe"
    native.parent.mkdir(parents=True)
    native.write_text("", encoding="utf-8")
    assert ".cmd" in SHIMS
    if os.name != "nt":
        pytest.skip("PATHEXT shims are a Windows lookup")
    os.environ["PATH"] = str(tmp_path) + os.pathsep + os.environ["PATH"]
    try:
        [found] = resolve_command(["claude"])
        assert found.casefold() == str(native).casefold()
    finally:
        os.environ["PATH"] = os.environ["PATH"].split(os.pathsep, 1)[1]


def test_a_shim_with_nothing_behind_it_is_refused_never_run_through_a_shell(
    tmp_path: Path,
) -> None:
    from lucy_coder.runner import ClaudeNotFoundError, resolve_command

    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    with pytest.raises(ClaudeNotFoundError, match=r"only as a script shim \(claude.cmd\)"):
        resolve_command([str(shim)])


async def test_a_turn_and_the_doctor_both_say_a_shim_is_not_enough(
    tmp_path: Path, workdir: str
) -> None:
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    runner = ClaudeRunner([str(shim)], budget_usd=0.5, timeout_seconds=5)
    outcome = await runner.run_turn(
        task_id="tsk_1",
        prompt="anything & del everything",
        cwd=workdir,
        session_id="ses-1",
        resume=False,
        run_level="edits",
        counters=Counters(),
        transcribe=lambda _line: None,
    )
    assert not outcome.ok
    assert "script shim" in outcome.detail
    assert "script shim" in doctor([str(shim)])


async def test_a_found_program_that_will_not_execute_reads_as_not_installed(
    tmp_path: Path, workdir: str
) -> None:
    """Found on the path but corrupt, or not a program at all: the OS refuses to run it,
    and the person reads the same sentence as a missing install."""
    broken = tmp_path / "claude-broken.exe"
    broken.write_text("this is not a program", encoding="utf-8")
    broken.chmod(0o755)  # executable by mode, so POSIX lookup finds it and exec refuses it
    runner = ClaudeRunner([str(broken)], budget_usd=0.5, timeout_seconds=5)
    outcome = await runner.run_turn(
        task_id="tsk_1",
        prompt="anything",
        cwd=workdir,
        session_id="ses-1",
        resume=False,
        run_level="edits",
        counters=Counters(),
        transcribe=lambda _line: None,
    )
    assert outcome.detail == NOT_INSTALLED
    assert doctor([str(broken)]) == NOT_INSTALLED
