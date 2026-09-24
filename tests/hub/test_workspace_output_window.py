"""A long command output shows its end, where the verdict is, and says which end it shows.

`workspace.run` handed the model the first 64 KiB of whatever a command printed. A test run
or a build prints its verdict last, so the one line the model was asked about -- "3 failed",
"error: linker command failed" -- was in the part it never saw. The sandbox can now return
either end (`output_window`) and counts the bytes its cap left out; the hub asks for the
end unless the model asks for the start, and reads the count when the sandbox sends one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from test_workspace_exec import (
    COMMAND,
    MIB,
    a_megabyte_log,
    a_workspace,
    exec_answer,
    run_step,
)

from lucy_api.clients.environments import (
    DEFAULT_OUTPUT_BYTES,
    FakeEnvironmentsClient,
    HttpEnvironmentsClient,
    Ran,
)
from lucy_api.clients.testing import Answer, FakeHttp
from lucy_api.packs.workspace import MAX_TOOL_OUTPUT_CHARS

if TYPE_CHECKING:
    from collections.abc import Mapping

VERDICT = "FAILED tests/test_calc.py::test_divide - ZeroDivisionError\n3 failed, 41 passed"


async def _ran(answer: Mapping[str, Any], *, tail: bool) -> tuple[Ran, dict[str, Any]]:
    http = FakeHttp(Answer(body=dict(answer)))
    ran = await HttpEnvironmentsClient(http, "http://environments.test").run(
        "env-1", COMMAND, tail=tail
    )
    return ran, http.last.json


def the_end_of_a_megabyte_log() -> dict[str, Any]:
    """What a sandbox that keeps the tail answers: the last 64 KiB, and how much came before."""
    return exec_answer(
        exit_code=1,
        output="." * (DEFAULT_OUTPUT_BYTES - len(VERDICT)) + VERDICT,
        output_start=120,
        output_end=120 + MIB,
        log_bytes=MIB,
        output_cursor=120 + MIB,
        output_truncated_bytes=MIB - DEFAULT_OUTPUT_BYTES,
    )


async def test_a_long_run_shows_the_model_its_verdict() -> None:
    """The bug, named: the model saw the first 8,000 characters of the run and not its end."""
    fake = FakeEnvironmentsClient()
    printed = "." * MIB + VERDICT
    fake.script(COMMAND, Ran(command=COMMAND, exit_code=1, output=printed, state="exited"))
    capabilities, context = a_workspace(fake)

    step = await run_step(capabilities, context)

    earlier = len(printed) - MAX_TOOL_OUTPUT_CHARS
    assert step["output"].endswith(VERDICT)
    assert len(step["output"]) == MAX_TOOL_OUTPUT_CHARS
    assert step["notice"] == (
        f"{earlier} output characters or bytes omitted; "
        f"this is the end of the output, and {earlier} of those came before it"
    )


async def test_the_client_asks_for_the_end_only_when_told_to() -> None:
    _ran_end, asked_end = await _ran(the_end_of_a_megabyte_log(), tail=True)
    _ran_start, asked_start = await _ran(exec_answer(), tail=False)
    assert asked_end["output_window"] == "tail"
    assert asked_start["output_window"] == "head"


async def test_a_sandbox_that_keeps_the_end_is_read_by_its_own_count() -> None:
    ran, _asked = await _ran(the_end_of_a_megabyte_log(), tail=True)
    assert ran.tail is True
    assert ran.output.endswith(VERDICT)
    assert ran.output_truncated_bytes == MIB - DEFAULT_OUTPUT_BYTES


async def test_a_sandbox_from_before_the_window_is_read_as_the_beginning() -> None:
    """It ignores `output_window` and counts nothing, so what came back is the head, and the
    cut is in the offsets. Calling that the end would be a confident wrong fact."""
    ran, _asked = await _ran(a_megabyte_log(), tail=True)
    assert ran.tail is False
    assert ran.output_truncated_bytes == MIB - DEFAULT_OUTPUT_BYTES


async def test_output_that_fits_is_shown_whole_with_no_notice() -> None:
    fake = FakeEnvironmentsClient()
    fake.script(COMMAND, Ran(command=COMMAND, exit_code=0, output="3 passed", state="exited"))
    capabilities, context = a_workspace(fake)

    step = await run_step(capabilities, context)

    assert (step["output"], step["notice"]) == ("3 passed", "")


async def test_the_fake_keeps_the_end_it_was_asked_for_and_counts_the_rest() -> None:
    fake = FakeEnvironmentsClient()
    fake.script(COMMAND, Ran(command=COMMAND, output="abcdef", state="exited"))
    end = await fake.run("env-1", COMMAND, max_output_bytes=2, tail=True)
    start = await fake.run("env-1", COMMAND, max_output_bytes=2)
    assert (end.output, end.output_truncated_bytes, end.tail) == ("ef", 4, True)
    assert (start.output, start.output_truncated_bytes, start.tail) == ("ab", 4, False)
