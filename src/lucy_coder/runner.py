"""One Claude Code turn as a subprocess: spawn, stream, account, stop.

The CLI's headless contract (verified 2026-10-08 on 2.1.280): with
``--output-format stream-json --verbose`` it emits one JSON object per line -- a
``system/init`` record naming the session, ``assistant`` records per content block, and one
final ``result`` record carrying ``total_cost_usd``, ``num_turns``, ``is_error``,
``subtype`` and the answer text. ``--resume`` on a busy session does not refuse; it runs a
second turn concurrently on the same transcript, so the service above this never starts
two turns on one task.

Everything that can end a turn ends it with a sentence: the binary missing, the login
missing, a session limit, the budget cap (``error_max_budget_usd``), the bridge's own wall
clock. A person reads the sentence on the task row; nothing hangs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_coder.tasks import DENIAL_INPUT_CHARS

if TYPE_CHECKING:
    from collections.abc import Callable

RUN_LEVELS = {
    "plan": "plan",
    "ask": "default",
    "edits": "acceptEdits",
    "full": "bypassPermissions",
}
"""Claude Code's permission modes as Lucy names them, mapped to `--permission-mode`.

`ask` is Claude Code's default mode. Headless there is nobody to answer its prompts, so a
tool that needs permission is refused and reported (`permission_denials`); the person's yes
to that tool comes back as `--allowedTools` on the next turn. That is the normal user's
"approve the prompt", with a card instead of a keypress."""

MODES = ("plan", "ask", "edits", "full")
"""The modes from most to least careful. The person's `claude_code_run_level` is a ceiling
on this order, which the hub enforces before any call reaches the bridge."""

NOT_INSTALLED = (
    "Claude Code is not installed on this machine, or is not on PATH; install it and run"
    " `claude` once to sign in"
)
TIMED_OUT = "ran past the bridge's {minutes:.0f}-minute ceiling and was stopped"
NO_ANSWER = "the claude process ended (exit {code}) without a result record; its last words: {said}"
BUDGET_SPENT = (
    "stopped by the bridge's ${budget:.2f} per-turn budget (CODER_TURN_BUDGET_USD) before it "
    "finished; a message resumes it, and the next turn gets a fresh budget"
)

SIGN_IN_MARKERS = ("not logged in", "/login", "sign in", "authentication_error", "api key")
"""Fragments of the CLI's own wording for a missing login, matched case-insensitively.

Matched against the error text the CLI itself produced, to choose a *clearer* sentence for
the person -- never to decide whether something failed. The turn is already failed either
way, so a marker that drifts costs clarity, not correctness.
"""

SIGNED_OUT = "Claude Code is signed out on this machine; run `claude` once to sign in"

ONLY_A_SHIM = (
    "Claude Code was found only as a script shim ({shim}), and the bridge never runs a "
    "brief through a shell; set CODER_CLAUDE_COMMAND to the claude executable itself"
)

SHIMS = frozenset({".cmd", ".bat", ".ps1"})
"""Script launchers the bridge refuses to run. Through cmd.exe, a brief the model wrote
would meet `&`, `|`, `^` and `%` as syntax -- command injection on the person's machine."""

NPM_NATIVE = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin"
"""Where npm's `claude.cmd` shim keeps the native binary it forwards to, beside the shim."""


class ClaudeNotFoundError(Exception):
    """No runnable claude; the message is the sentence a person should read."""


def resolve_command(command: list[str]) -> list[str]:
    """The configured command with its program made something runnable without a shell.

    A bare name is looked up on PATH. A native executable is used as it is. An npm script
    shim (`claude.cmd` on Windows) is followed to the native binary it forwards to; a shim
    with no binary behind it is refused with a sentence, never run through cmd.exe.

    Raises:
        ClaudeNotFoundError: nothing runnable, with the sentence that says why.
    """
    program, *rest = command
    found = shutil.which(program)
    if found is None:
        raise ClaudeNotFoundError(NOT_INSTALLED)
    path = Path(found)
    if path.suffix.lower() not in SHIMS:
        return [str(path), *rest]
    native = path.parent / NPM_NATIVE / f"{path.stem}.exe"
    if native.is_file():
        return [str(native), *rest]
    raise ClaudeNotFoundError(ONLY_A_SHIM.format(shim=path.name))


@dataclass
class TurnOutcome:
    """How one turn ended, with everything the task row keeps."""

    ok: bool
    detail: str = ""
    result: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    session_id: str = ""
    denials: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Counters:
    """Live progress, written as events arrive; the service copies them onto the row."""

    tool_uses: int = 0
    last_tool: str = ""
    last_text: str = ""
    events: int = 0
    on_change: Callable[[Counters], None] | None = None

    def saw(self, event: dict[str, Any]) -> None:
        self.events += 1
        for block in _blocks(event):
            kind = str(block.get("type") or "")
            if kind == "tool_use":
                self.tool_uses += 1
                self.last_tool = str(block.get("name") or "")
            elif kind == "text" and str(block.get("text") or "").strip():
                self.last_text = str(block["text"]).strip()
        if self.on_change is not None:
            self.on_change(self)


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return (
        [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    )


class ClaudeRunner:
    """Runs turns and kills them; owns nothing but the process it is currently watching."""

    def __init__(self, command: list[str], *, budget_usd: float, timeout_seconds: float) -> None:
        self._command = command
        self._budget = budget_usd
        self._timeout = timeout_seconds
        self._live: dict[str, asyncio.subprocess.Process] = {}

    async def run_turn(  # noqa: PLR0913 - one turn: what to say, where, as whom, how far
        self,
        *,
        task_id: str,
        prompt: str,
        cwd: str,
        session_id: str,
        resume: bool,
        run_level: str,
        counters: Counters,
        transcribe: Callable[[str], None],
        model: str = "",
        allow_tools: tuple[str, ...] = (),
    ) -> TurnOutcome:
        """One ``claude -p`` turn, streamed into ``transcribe`` and ``counters``."""
        try:
            program = resolve_command(self._command)
        except ClaudeNotFoundError as exc:
            return TurnOutcome(ok=False, detail=str(exc), session_id=session_id)
        argv = [
            *program,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-budget-usd",
            str(self._budget),
            "--permission-mode",
            RUN_LEVELS.get(run_level, RUN_LEVELS["edits"]),
            *(["--resume", session_id] if resume else ["--session-id", session_id]),
            *(["--model", model] if model else []),
            # One comma-separated argument: the flag is variadic, and a list would swallow
            # whatever followed it.
            *(["--allowedTools", ",".join(allow_tools)] if allow_tools else []),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            return TurnOutcome(ok=False, detail=NOT_INSTALLED, session_id=session_id)
        self._live[task_id] = process
        try:
            return await asyncio.wait_for(
                self._watch(process, session_id, counters, transcribe),
                timeout=self._timeout,
            )
        except TimeoutError:
            await self._kill(process)
            minutes = self._timeout / 60
            return TurnOutcome(
                ok=False, detail=TIMED_OUT.format(minutes=minutes), session_id=session_id
            )
        finally:
            self._live.pop(task_id, None)

    async def cancel(self, task_id: str) -> bool:
        """Kill a live turn's whole process tree. True when there was one to kill."""
        process = self._live.get(task_id)
        if process is None:
            return False
        await self._kill(process)
        return True

    async def _watch(
        self,
        process: asyncio.subprocess.Process,
        session_id: str,
        counters: Counters,
        transcribe: Callable[[str], None],
    ) -> TurnOutcome:
        final: dict[str, Any] | None = None
        last_raw = ""
        assert process.stdout is not None  # noqa: S101 - PIPE above; typing narrows
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            transcribe(line)
            last_raw = line
            try:
                event = json.loads(line)
            except ValueError:
                # The CLI writes warnings and login prompts as plain text on the same pipe.
                continue
            counters.saw(event)
            if event.get("type") == "result":
                final = event
        code = await process.wait()
        if final is None:
            said = _clip(last_raw, 200) or "nothing"
            detail = SIGNED_OUT if _looks_signed_out(last_raw) else ""
            return TurnOutcome(
                ok=False,
                detail=detail or NO_ANSWER.format(code=code, said=said),
                session_id=session_id,
            )
        return self._ended(final, session_id)

    def _ended(self, final: dict[str, Any], session_id: str) -> TurnOutcome:
        cost = float(final.get("total_cost_usd") or 0.0)
        turns = int(final.get("num_turns") or 0)
        session = str(final.get("session_id") or session_id)
        denials = _denials(final.get("permission_denials"))
        if not final.get("is_error"):
            return TurnOutcome(
                ok=True,
                result=str(final.get("result") or ""),
                cost_usd=cost,
                num_turns=turns,
                session_id=session,
                denials=denials,
            )
        subtype = str(final.get("subtype") or "error")
        said = _clip(str(final.get("result") or ""), 400)
        if subtype == "error_max_budget_usd":
            detail = BUDGET_SPENT.format(budget=self._budget)
        elif _looks_signed_out(said):
            detail = SIGNED_OUT
        else:
            # The CLI's own sentence ("You've hit your session limit · resets 1am") is the
            # honest one; the subtype alone would hide it.
            detail = said or subtype
        return TurnOutcome(
            ok=False,
            detail=detail,
            result=said,
            cost_usd=cost,
            num_turns=turns,
            session_id=session,
            denials=denials,
        )

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        """Take down the whole tree. `terminate` alone leaves grandchildren running."""
        if process.returncode is not None:
            return
        await asyncio.to_thread(TREE_KILL, process.pid)
        with contextlib.suppress(ProcessLookupError):
            # The sweep usually got there first; a dead process needs no second blow.
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10)


def taskkill_tree(pid: int) -> None:
    """Windows: take the whole tree down by PID; verified to leave no stray node.

    Harmless anywhere else -- the binary is missing and the miss is swallowed -- because
    the caller follows with ``process.kill()``, which every platform has. On POSIX that
    orphans grandchildren the CLI may have spawned; the bridge runs on this Windows host,
    and a POSIX deployment would swap this for a process group.
    """
    with contextlib.suppress(OSError):
        subprocess.run(  # noqa: S603 - a fixed command and our own pid
            ["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S607 - the system's own tool
            capture_output=True,
            check=False,
        )


def no_tree_kill(pid: int) -> None:
    """POSIX: nothing extra before ``process.kill()``; named so the wiring reads."""


TREE_KILL = taskkill_tree if sys.platform == "win32" else no_tree_kill


def doctor(command: list[str], *, timeout_seconds: float = 30) -> str:
    """One sentence on whether the CLI answers, for `/ready`. Empty means healthy."""
    try:
        program = resolve_command(command)
    except ClaudeNotFoundError as exc:
        return str(exc)
    try:
        probe = subprocess.run(  # noqa: S603 - the resolved binary, --version only
            [*program, "--version"],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            text=True,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return NOT_INSTALLED
    except subprocess.TimeoutExpired:
        return f"`claude --version` did not answer within {timeout_seconds:.0f} seconds"
    if probe.returncode != 0:
        return f"`claude --version` failed (exit {probe.returncode})"
    return ""


def _denials(raw: object) -> list[dict[str, str]]:
    """What the turn was refused, as the row keeps it: the tool and a clipped input."""
    if not isinstance(raw, list):
        return []
    kept: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        given = item.get("tool_input")
        shown = json.dumps(given, ensure_ascii=False) if given is not None else ""
        kept.append(
            {
                "tool": str(item.get("tool_name") or ""),
                "input": _clip(shown, DENIAL_INPUT_CHARS),
            }
        )
    return kept


def _looks_signed_out(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SIGN_IN_MARKERS)


def _clip(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "\N{HORIZONTAL ELLIPSIS}"
