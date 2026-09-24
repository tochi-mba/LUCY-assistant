"""One command, from the sandbox's answer to what the model is told about it.

`POST /v1/exec` is the one call where the hub and the sandbox disagreed about what a field
meant, and every disagreement reached the model as a confident wrong fact. So the answers
here are built from the sandbox's own serialisers rather than from what the hub expects:
`CommandRecord.to_dict` (Environments-api app/shells/shell.py), `command_result`
(app/api/routes/shells.py) and `exec_once` (app/api/routes/exec.py), key for key.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lucy_api.clients.environments import (
    AUDIENCE,
    DEFAULT_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    EXEC_MARGIN_SECONDS,
    Environment,
    FakeEnvironmentsClient,
    HttpEnvironmentsClient,
    Ran,
)
from lucy_api.clients.testing import Answer, FakeHttp, ReadRecorder
from lucy_api.packs.http import UNREACHABLE, DownstreamUnavailableError
from lucy_api.packs.service import Capabilities
from lucy_api.packs.watch import _command_check
from lucy_api.packs.workspace import MAX_TOOL_OUTPUT_CHARS, WorkspacePack
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.work import Registry

if TYPE_CHECKING:
    from lucy_api.packs.context import PackContext

COMMAND = "pytest -q"
MIB = 1024 * 1024


def exec_answer(**overrides: Any) -> dict[str, Any]:
    """What `POST /v1/exec` sends back for one command, with every key it really sends.

    `CommandRecord.to_dict` (app/shells/shell.py) supplies the record, `command_result`
    (app/api/routes/shells.py) adds the output and its cursor, and `exec_once`
    (app/api/routes/exec.py) adds the shell's state after closing it and the credential
    lists. `command` is the wrapped string the sandbox actually ran, not the one it was sent.
    """
    answer: dict[str, Any] = {
        "id": "cmd_1",
        "shell_id": "sh_1",
        "environment_id": "env-1",
        "command": f"( {COMMAND}\n)",
        "state": "exited",
        "exit_code": 0,
        "started_at": 1_790_000_000.0,
        "finished_at": 1_790_000_002.5,
        "output_start": 120,
        "output_end": 128,
        "log_bytes": 8,
        "log_truncated": False,
        "timeout_ms": 60_000,
        "output": "3 passed",
        "output_dropped_bytes": 0,
        "output_cursor": 128,
        "shell_state": "closed",
        "credentials_injected": [],
        "credentials_missing": [],
    }
    return {**answer, **overrides}


def killed_at_its_ceiling() -> dict[str, Any]:
    """A command the sandbox killed: `state` says so, and nothing else on the wire does.

    `_on_timeout` sets the record's `timed_out` and SIGKILLs the command's children, the
    subshell reports 137, and `_finish` turns the flag into `CommandState.TIMED_OUT`
    (app/shells/shell.py). The flag itself is never serialised.
    """
    return exec_answer(
        state="timed_out",
        exit_code=137,
        output="collected 812 items\n....",
        output_end=144,
        log_bytes=24,
        output_cursor=144,
    )


def a_megabyte_log(**overrides: Any) -> dict[str, Any]:
    """A build that printed a megabyte, of which the sandbox returned the first 64 KiB.

    `command_result` reads from `output_start` for at most `max_output_bytes`, so
    `output_cursor` stops 64 KiB in while `output_end` is a megabyte on. The ring buffer lost
    nothing, so `output_dropped_bytes` is zero: it never counted this cut.
    """
    start = 120
    answer = exec_answer(
        exit_code=1,
        output="." * DEFAULT_OUTPUT_BYTES,
        output_start=start,
        output_end=start + MIB,
        log_bytes=MIB,
        output_cursor=start + DEFAULT_OUTPUT_BYTES,
    )
    return {**answer, **overrides}


async def ran_from(answer: dict[str, Any]) -> Ran:
    http = FakeHttp(Answer(body=answer))
    return await HttpEnvironmentsClient(http, "http://environments.test").run("env-1", COMMAND)


def a_workspace(
    client: FakeEnvironmentsClient, *, work: Registry | None = None
) -> tuple[Capabilities, PackContext]:
    client.seed(Environment("env-1", "Conversation", profile="personal"))
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=client)], work=work)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    return capabilities, context


class Unhurried(FakeEnvironmentsClient):
    """A sandbox that answers when the test says so, as one closing a shell after a kill does."""

    def __init__(self) -> None:
        super().__init__()
        self.answer = asyncio.Event()

    async def run(
        self,
        environment_id: str,
        command: str,
        *,
        cwd: str = ".",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> Ran:
        await self.answer.wait()
        return await super().run(
            environment_id,
            command,
            cwd=cwd,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
        )


class Unreachable(FakeEnvironmentsClient):
    """A sandbox whose answer never came: what the exec call raises when its timeout fires."""

    async def run(
        self,
        environment_id: str,
        command: str,
        *,
        cwd: str = ".",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_OUTPUT_BYTES,
    ) -> Ran:
        raise DownstreamUnavailableError(UNREACHABLE, audience=AUDIENCE)


def a_registry() -> Registry:
    return Registry(now=lambda: datetime.now(UTC))


async def run_step(
    capabilities: Capabilities, context: PackContext, **inputs: Any
) -> dict[str, Any]:
    await capabilities.probe(context)
    plan = {
        "steps": [{"id": "run", "op": "workspace.run", "input": {"command": COMMAND, **inputs}}]
    }
    result = await capabilities.execute(plan, context)
    assert not result["issues"]
    step: dict[str, Any] = result["steps"][0]["data"]
    return step


# --- a command killed at its ceiling ----------------------------------------------------------


async def test_a_command_the_sandbox_killed_at_its_ceiling_reads_as_timed_out() -> None:
    """The bug, named: the client read only a `timed_out` key the sandbox never sends, so
    every killed command came back `timed_out: false` beside `state: "timed_out"`."""
    ran = await ran_from(killed_at_its_ceiling())

    assert ran.timed_out is True
    assert ran.state == "timed_out"
    assert ran.exit_code == 137


async def test_a_command_that_exited_is_not_a_timeout_and_the_flag_still_counts_if_sent() -> None:
    assert (await ran_from(exec_answer())).timed_out is False
    assert (await ran_from(exec_answer(state="exited", timed_out=True))).timed_out is True


async def test_the_timeout_flag_is_the_only_key_the_client_reads_ahead_of_the_sandbox() -> None:
    """Every other key the client asks for is one the sandbox's serialisers send. `timed_out`
    is asked for on purpose, so a sandbox that starts sending it is understood at once."""
    answer = ReadRecorder(killed_at_its_ceiling())

    await ran_from(answer)

    assert answer.absent == {"timed_out"}


async def test_the_fake_reads_a_timed_out_state_the_way_the_client_does() -> None:
    fake = FakeEnvironmentsClient()
    fake.script(COMMAND, Ran(command=COMMAND, exit_code=137, state="timed_out"))

    assert (await fake.run("env-1", COMMAND)).timed_out is True


async def test_a_command_watch_waits_on_a_killed_command_rather_than_judging_its_output() -> None:
    """What the timeout flag is for: a watch whose command was killed has not seen its answer.
    Before, the killed command fell through to the pattern and fired on partial output."""
    http = FakeHttp(Answer(body=killed_at_its_ceiling()))
    client = HttpEnvironmentsClient(http, "http://environments.test")

    check = await _command_check(client, "env-1", "sessions/s", COMMAND, re.compile("items"))

    assert check.fired is False
    assert check.detail == "command timed out"


# --- output past the sandbox's cap ------------------------------------------------------------


async def test_output_the_sandbox_did_not_return_is_counted_from_its_byte_offsets() -> None:
    """The bug, named: the sandbox returns the head of the output and says how much followed
    only in `output_end` and `output_cursor`, which the client never read, so a megabyte cut
    was counted as nothing."""
    ran = await ran_from(a_megabyte_log())

    assert ran.output_dropped_bytes == 0
    assert ran.output_truncated_bytes == MIB - DEFAULT_OUTPUT_BYTES


async def test_a_command_with_no_end_yet_counts_no_cut_rather_than_guessing_one() -> None:
    assert (await ran_from(a_megabyte_log(output_end=None))).output_truncated_bytes == 0
    no_cursor = a_megabyte_log()
    del no_cursor["output_cursor"]
    assert (await ran_from(no_cursor)).output_truncated_bytes == 0
    assert (await ran_from(exec_answer())).output_truncated_bytes == 0


async def test_the_fake_returns_the_head_of_long_output_and_counts_the_rest() -> None:
    """Cut at a byte count and decoded with replacement, as `command_result` does it."""
    fake = FakeEnvironmentsClient()
    fake.script(COMMAND, Ran(command=COMMAND, exit_code=0, output="\u00e9" * 3, state="exited"))

    ran = await fake.run("env-1", COMMAND, max_output_bytes=3)

    assert ran.output == "\u00e9\ufffd"
    assert ran.output_truncated_bytes == 3


async def test_the_model_is_told_it_has_the_beginning_and_how_much_came_after_it() -> None:
    fake = FakeEnvironmentsClient()
    fake.script(COMMAND, Ran(command=COMMAND, exit_code=1, output="." * MIB, state="exited"))
    capabilities, context = a_workspace(fake)

    step = await run_step(capabilities, context)

    later = MIB - MAX_TOOL_OUTPUT_CHARS
    assert len(step["output"]) == MAX_TOOL_OUTPUT_CHARS
    assert step["output_dropped_bytes"] == later
    assert step["notice"] == (
        f"{later} output characters or bytes omitted; "
        f"this is the beginning of the output, and {later} of those came after it"
    )


async def test_output_lost_only_before_the_read_does_not_claim_a_later_cut() -> None:
    fake = FakeEnvironmentsClient()
    fake.script(
        COMMAND,
        Ran(command=COMMAND, exit_code=0, output="ok", output_dropped_bytes=7, state="exited"),
    )
    capabilities, context = a_workspace(fake)

    step = await run_step(capabilities, context)

    assert step["notice"] == "7 output characters or bytes omitted"


async def test_a_command_watch_says_its_pattern_was_only_looked_for_in_the_head() -> None:
    """A verdict printed after the first 64 KiB never comes back, so "no match" alone would
    read as "not finished yet" about a build that may have finished and failed."""
    http = FakeHttp(Answer(body=a_megabyte_log()))
    client = HttpEnvironmentsClient(http, "http://environments.test")

    check = await _command_check(client, "env-1", "sessions/s", COMMAND, re.compile("failed"))

    assert check.fired is False
    assert check.detail == "exit 1, no match; 983,040 later bytes unread"


# --- a deadline that outlasts the call it wraps -----------------------------------------------


async def test_a_command_answered_after_its_own_ceiling_still_reaches_the_model() -> None:
    """The bug, named: the work deadline was the command's own ceiling, shorter than the exec
    call around it, so a command the sandbox killed at its ceiling was cancelled here while
    the sandbox was still closing its shell, and the model got `result: null`."""
    fake = Unhurried()
    fake.script(COMMAND, Ran(command=COMMAND, exit_code=137, output="....", state="timed_out"))
    work = a_registry()
    capabilities, context = a_workspace(fake, work=work)
    try:
        step = asyncio.create_task(run_step(capabilities, context, timeout_ms=1))
        await asyncio.sleep(0.05)
        fake.answer.set()
        answered = await step
    finally:
        await work.shutdown()

    assert answered["state"] == "timed_out"
    assert answered["timed_out"] is True
    assert answered["output"] == "...."


async def test_the_work_deadline_outlasts_the_exec_call_it_wraps() -> None:
    fake = Unhurried()
    work = a_registry()
    capabilities, context = a_workspace(fake, work=work)
    try:
        started = await run_step(capabilities, context, timeout_ms=30_000, wait=False)
        running = work.running("sess-a")
    finally:
        fake.answer.set()
        await work.shutdown()

    assert started["status"] == "running"
    assert running[0].timeout_seconds > 30 + EXEC_MARGIN_SECONDS


async def test_a_command_with_no_answer_says_how_it_ended_instead_of_result_null() -> None:
    """When the exec call's own timeout fires, the work fails with the error's name, and that
    is what the model is told. It used to see only `{"work_id": ..., "result": null}`."""
    work = a_registry()
    capabilities, context = a_workspace(Unreachable(), work=work)
    try:
        step = await run_step(capabilities, context)
    finally:
        await work.shutdown()

    assert step == {
        "status": "failed",
        "work_id": step["work_id"],
        "notice": "DownstreamUnavailableError",
    }
