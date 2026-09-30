"""Commands a scenario runs on this machine, between two turns, through the shell.

A conversation sometimes needs the world to change between two things the person says in a
way the hub cannot and should not arrange: a sibling service stopped, to see how Lucy copes
with the outage, and started again a turn later. A scenario says so with a ``host`` step,
and the harness runs it here. A scenario file would otherwise be a way to run anything on
the operator's machine, so ``lucy eval run`` hands the runner a :data:`Shell` only when it
was started with ``--allow-host``, and a runner with no shell refuses every command.

The command goes to the system shell -- ``/bin/sh`` on Linux and macOS, ``cmd.exe`` on
Windows -- with nothing on its standard input. Its output and errors go, interleaved as they
were written, to a temporary file rather than a pipe: a pipe still held open by something
the command started in the background would keep the harness waiting after the command
itself had ended. At its timeout the shell is killed; what it started in the background may
outlive it.

Only the last :data:`OUTPUT_LIMIT` bytes of what it printed are kept, behind a notice of
exactly how many were not: the end of the output is where a failure says why, and a report
is no place for a build log.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from lucy_api.evals.scenario import OK

if TYPE_CHECKING:
    from collections.abc import Callable

OUTPUT_LIMIT = 4_000
"""Bytes of a command's output that are kept, counted from the end."""

SHOWN = 160
"""Characters of that output a reason shows, from the end; the report keeps the rest."""

TIMED_OUT = "timed out"
REFUSED = "refused"
NOT_ALLOWED = "this run was not allowed to run commands on this machine"


@dataclass(frozen=True, slots=True)
class Finished:
    """How one command ended."""

    exit_code: int | None
    """Its exit status, or ``None`` when it was still running at its timeout and stopped."""

    output: str
    """What it printed, standard output and error as they interleaved, kept from the end."""

    @property
    def status(self) -> str:
        """``ok``, ``exit <code>`` or ``timed out``: how the step's record says it ended."""
        if self.exit_code is None:
            return TIMED_OUT
        return OK if self.exit_code == 0 else f"exit {self.exit_code}"


type Shell = Callable[[str, float], Finished]
"""Runs one command through this machine's shell, stopping it after so many seconds."""


def run_in_shell(command: str, timeout: float) -> Finished:
    """The real :data:`Shell`: bounded in time, and in how much of what it prints is kept."""
    with tempfile.TemporaryFile() as sink:
        try:
            ended = subprocess.run(  # noqa: S602 - the operator's own command, under --allow-host
                command,
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Finished(exit_code=None, output=tail(sink))
        return Finished(exit_code=ended.returncode, output=tail(sink))


def tail(sink: IO[bytes], limit: int = OUTPUT_LIMIT) -> str:
    """The last ``limit`` bytes written to ``sink``, as text, and how many came before them.

    Bytes that are not UTF-8 are shown as the replacement character rather than refused:
    a command's output is evidence, and a stray byte must not cost the rest of it.
    """
    size = sink.seek(0, os.SEEK_END)
    sink.seek(max(0, size - limit))
    kept = sink.read().decode("utf-8", errors="replace")
    if size <= limit:
        return kept
    return f"[showing the last {limit:,} of {size:,} bytes]\n{kept}"


def failure(finished: Finished, timeout: float) -> str:
    """How a command that did not succeed ended, with the last of what it printed."""
    how = (
        f"timed out after {timeout:g}s"
        if finished.exit_code is None
        else f"exited {finished.exit_code}"
    )
    last = _last_words(finished.output)
    return f"{how}: {last}" if last else how


def _last_words(output: str) -> str:
    """The end of ``output`` on one line, marked where it was cut."""
    flat = " ".join(output.split())
    return flat if len(flat) <= SHOWN else "…" + flat[-(SHOWN - 1) :]


__all__ = [
    "NOT_ALLOWED",
    "OUTPUT_LIMIT",
    "REFUSED",
    "SHOWN",
    "TIMED_OUT",
    "Finished",
    "Shell",
    "failure",
    "run_in_shell",
    "tail",
]
