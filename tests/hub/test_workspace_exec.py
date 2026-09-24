"""One command, from the sandbox's answer to what the model is told about it.

`POST /v1/exec` is the one call where the hub and the sandbox disagreed about what a field
meant, and every disagreement reached the model as a confident wrong fact. So the answers
here are built from the sandbox's own serialisers rather than from what the hub expects:
`CommandRecord.to_dict` (Environments-api app/shells/shell.py), `command_result`
(app/api/routes/shells.py) and `exec_once` (app/api/routes/exec.py), key for key.
"""

from __future__ import annotations

import re
from typing import Any

from lucy_api.clients.environments import FakeEnvironmentsClient, HttpEnvironmentsClient, Ran
from lucy_api.clients.testing import Answer, FakeHttp, ReadRecorder
from lucy_api.packs.watch import _command_check

COMMAND = "pytest -q"


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
        output_end=145,
        log_bytes=25,
        output_cursor=145,
    )


async def ran_from(answer: dict[str, Any]) -> Ran:
    http = FakeHttp(Answer(body=answer))
    return await HttpEnvironmentsClient(http, "http://environments.test").run("env-1", COMMAND)


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
