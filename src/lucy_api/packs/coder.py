"""Claude Code: hand a whole task to a real session on the person's machine.

Not clyde, and not a helper. clyde is a model provider; a helper is read-only and dies with
its report. A delegation is a discrete task with a written brief, run by Claude Code on the
person's own computer, in a folder they listed, at a run level they chose -- and it stays
alive afterwards, resumable with a message from any conversation. ADR-0017 holds the
design; the host half is ``src/lucy_coder`` in this repository.

Three switches, none of them Lucy's. The operator deploys the bridge and points
``coder_api_base_url`` at it, or this capability is absent. The person turns
``lucy.claude_code_delegation`` on and lists ``claude_code_directories``, or it reports
disabled and says whose switch it is. And every task needs their yes to that task alone
(``each_call``): neither ``auto`` nor a standing grant covers the next one, because each
card is a brief about to act in their name on their machine.

The hub keeps track the way it keeps track of everything that outlives a step: each
delegated turn is a piece of work (``Kind.job``) that polls the bridge until the task
settles, wakes the session with the person's standing consent, and narrates hub-written
counter sentences -- never Claude Code's own words -- in the live block. What comes back is
a report from another program: untrusted, framed, data.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import (
    array_schema,
    enum_schema,
    integer_schema,
    object_schema,
    string_schema,
)
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.coder import AUDIENCE, CoderTask, HttpCoderClient
from lucy_api.clients.errors import DownstreamError
from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc
from lucy_api.work import Brief, Kind, new_id
from lucy_api.work.subscriptions import waking_tags
from lucy_api.work.types import WorkError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.coder import CoderClient
    from lucy_api.packs.context import PackContext
    from lucy_api.work.registry import Registry

SERVICE = "coder"

POLL_SECONDS = 5.0
"""How often the work item asks the bridge whether the task has settled. Polling matches
watches; the bridge is one small GET away and signals would need a per-sibling signal URL
(rejected for v1 in ADR-0017)."""

POLL_MISSES = 12
"""Consecutive unanswered polls -- a minute at five seconds -- before tracking gives up."""

BRIDGE_SILENT = (
    "the Claude Code bridge stopped answering, so tracking stopped; the task may still be "
    "running on their machine. coder.read {task_id} checks it once the bridge is back."
)

TASK_SECONDS = 50 * 60.0
"""The work item's own ceiling: past the bridge's 45-minute turn clock plus slack, so the
honest ending always comes from the bridge, and `timed_out` here means the bridge is gone."""

PERSONS_SWITCH = (
    "Claude Code delegation is switched off. It is the person's switch "
    "(lucy.claude_code_delegation, and the folders in lucy.claude_code_directories): tell "
    "them where, do not try to change it."
)
NO_DIRECTORIES = (
    "No folders are listed in lucy.claude_code_directories, so nothing may be delegated. "
    "The list is the person's alone: tell them where, do not try to change it."
)
NOT_ALLOWED_THERE = (
    "{directory} is not among the folders the person listed for Claude Code. Their list: "
    "{allowed}. Ask them, or use a listed folder; never work around the list."
)
STARTED = (
    "The task is {state} as {work_id}. A notice arrives when its turn ends; coder.read "
    "shows progress, coder.message steers it, coder.cancel stops it. Carry on, or finish "
    "your answer."
)
BRIEF_FIELD = (
    "The whole task, in your own words: the outcome that means done, the constraints, and "
    "anything already decided. It acts on the person's real computer, so a brief that "
    "quotes a page, a helper's report or a memory you did not write must say so beside "
    "the card."
)
DIRECTORY_FIELD = "One folder from the person's claude_code_directories list, written out."
TITLE_FIELD = "A short name for the task, for lists and the live block."

MODES = ("plan", "ask", "edits", "full")
"""Claude Code's permission modes, most careful first. `ask` is its own default mode: a
tool that needs permission is refused and brought back rather than prompted. The person's
`claude_code_run_level` is a ceiling on this order."""

ABOVE_CEILING = (
    "The person lets Claude Code run at most at `{ceiling}`; `{mode}` is above that. Use "
    "`{ceiling}` or a more careful mode, or tell them where to change the setting."
)
PLAN_ALLOWS_NOTHING = (
    "Plan mode is read-only, so allowing a tool does nothing there. Resume at `ask` or "
    "above to let it use what it was refused."
)
MODE_FIELD = (
    "Claude Code's permission mode for this turn: plan (read-only, proposes a plan), ask "
    "(anything needing permission is refused and brought back), edits (changes files in "
    "its folder), full (runs commands unprompted). At most the person's level; omitted, "
    "their level."
)
MODEL_FIELD = "A Claude model alias or name for this task; omit for their Claude Code default."
ALLOW_FIELD = (
    "Tools this turn may use that the last one was refused, as Claude Code names them: "
    '"Write", "Bash(npm test:*)". Only what the person said yes to.'
)
REFUSED_TOOLS = (
    "Claude Code was refused {count} tool call(s): {tools}. Tell the person what it wanted; "
    "with their yes, coder.message with allow_tools {rules} lets it carry on."
)

_LIVE = frozenset({"queued", "running"})


class CoderPack:
    """Delegate, steer, read and stop Claude Code tasks through the host bridge."""

    id = "coder"
    title = "Claude Code"
    summary = "Delegate a whole task to Claude Code on the person's machine, and track it."

    def __init__(
        self, base_url: str, *, audience: str = AUDIENCE, client: CoderClient | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="coder.delegate",
                title="Hand a task to Claude Code on your computer",
                description=(
                    "Start or steer a real Claude Code session in a folder you listed, "
                    "acting as you. Each task and each follow-up needs your yes to it."
                ),
                risk="execute",
                covers=("coder.delegate", "coder.message"),
                # Every call, in every mode: a standing yes cannot cover the next task,
                # because the next task is a different brief about to act in their name.
                each_call=lambda _arguments: True,
            ),
            Permission(
                id="coder.control",
                title="Stop a Claude Code task",
                description="Cancel a delegated task; what it already did stays done.",
                risk="write",
                covers=("coder.cancel",),
            ),
        )

    def result_trust(self, operation: str, data: object) -> Trust:
        """Everything here carries Claude Code's words somewhere: a result, a last line,
        a title echoed back. One program's output about the person's machine is data."""
        del operation, data
        return Trust.untrusted

    def setup(self) -> SetupPlan | None:
        # Deliberately none: there is no connect flow. The operator deploys the bridge and
        # the person flips their settings; an attach flow here would be a fourth switch.
        return None

    async def probe(self, context: PackContext) -> Availability:
        switched = _switched_off(self.base_url, context)
        if switched is not None:
            return switched
        try:
            readiness = await self._client(context).ready()
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(
                state=State.unavailable, detail="the Claude Code bridge could not be reached"
            )
        if not readiness.ready:
            return Availability(
                state=State.unavailable,
                detail=readiness.detail or "the Claude Code bridge is not ready",
            )
        return Availability(state=State.ready, detail="Claude Code is ready on their machine")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        level = context.policy.claude_code_run_level
        task = string_schema().describe("The task id coder.delegate returned.")
        return (
            define_operation(
                {
                    "name": "coder.delegate",
                    "description": (
                        "Hand one whole task to Claude Code on the person's machine, only "
                        f"when they asked for it. Their level is {level}; plan first when "
                        "the task is large, then carry the plan out with coder.message."
                    ),
                    "input": object_schema(
                        {
                            "brief": string_schema().describe(BRIEF_FIELD),
                            "directory": string_schema().describe(DIRECTORY_FIELD),
                            "title": string_schema().describe(TITLE_FIELD).optional(),
                            "mode": enum_schema(*MODES).describe(MODE_FIELD).optional(),
                            "model": string_schema().describe(MODEL_FIELD).optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._delegate,
                }
            ),
            define_operation(
                {
                    "name": "coder.message",
                    "description": (
                        "Send a follow-up into a delegated task's session: steer it, answer "
                        "its question, carry out its plan in another mode, or let it use a "
                        "tool it was refused. It runs when the current turn ends."
                    ),
                    "input": object_schema(
                        {
                            "task": task,
                            "text": string_schema().describe("What to tell it."),
                            "mode": enum_schema(*MODES).describe(MODE_FIELD).optional(),
                            "allow_tools": array_schema(string_schema())
                            .describe(ALLOW_FIELD)
                            .optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._message,
                }
            ),
            define_operation(
                {
                    "name": "coder.read",
                    "description": (
                        "One task's state, progress and final answer; `tail_chars` adds "
                        "the end of its raw transcript."
                    ),
                    "input": object_schema(
                        {
                            "task": task,
                            "tail_chars": integer_schema()
                            .describe("How much transcript to include.")
                            .optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._read,
                }
            ),
            define_operation(
                {
                    "name": "coder.list",
                    "description": (
                        "Every delegation, oldest first: state, folder, turns, cost. "
                        "Sessions outlive conversations."
                    ),
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._list,
                }
            ),
            define_operation(
                {
                    "name": "coder.cancel",
                    "description": (
                        "Stop a delegated task. What it already changed stays changed."
                    ),
                    "input": object_schema({"task": task}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._cancel,
                }
            ),
        )

    # ------------------------------------------------------------------ handlers

    async def _delegate(self, run: RunContext[PackContext]) -> dict[str, Any]:
        context = run.ctx
        brief = str(run.input.get("brief") or "").strip()
        directory = str(run.input.get("directory") or "").strip()
        title = str(run.input.get("title") or "").strip()
        if not brief:
            return {"status": "invalid", "message": "write the task as a brief, in sentences"}
        ceiling = context.policy.claude_code_run_level
        mode = str(run.input.get("mode") or ceiling)
        if not _within(mode, ceiling):
            return {
                "status": "refused",
                "message": ABOVE_CEILING.format(ceiling=ceiling, mode=mode),
            }
        allowed = _allowed(directory, context.policy.claude_code_directories)
        if allowed is None:
            return {
                "status": "refused",
                "message": NOT_ALLOWED_THERE.format(
                    directory=directory or "(no folder)",
                    allowed=", ".join(context.policy.claude_code_directories),
                ),
            }
        task = await self._client(context).start(
            brief=brief,
            directory=allowed,
            run_level=mode,
            title=title,
            model=str(run.input.get("model") or "").strip(),
        )
        return await self._tracked(context, task)

    async def _message(self, run: RunContext[PackContext]) -> dict[str, Any]:
        context = run.ctx
        task_id = str(run.input.get("task") or "").strip()
        text = str(run.input.get("text") or "").strip()
        if not task_id or not text:
            return {"status": "invalid", "message": "name the task, and say what to tell it"}
        client = self._client(context)
        ceiling = context.policy.claude_code_run_level
        asked = str(run.input.get("mode") or "")
        if asked and not _within(asked, ceiling):
            return {
                "status": "refused",
                "message": ABOVE_CEILING.format(ceiling=ceiling, mode=asked),
            }
        # Unsaid, the session keeps the mode it has -- unless the person has since lowered
        # their ceiling below it, and then it resumes at the ceiling.
        current = asked or (await client.get(task_id)).run_level
        mode = current if _within(current, ceiling) else ceiling
        allow = tuple(str(rule) for rule in run.input.get("allow_tools") or ())
        if allow and mode == "plan":
            return {"status": "refused", "message": PLAN_ALLOWS_NOTHING}
        changed = bool(asked) or mode != current
        task = await client.message(task_id, text, mode=mode if changed else "", allow_tools=allow)
        return await self._tracked(context, task)

    async def _read(self, run: RunContext[PackContext]) -> dict[str, Any]:
        context = run.ctx
        task_id = str(run.input.get("task") or "").strip()
        if not task_id:
            return {"status": "invalid", "message": "name the task coder.delegate returned"}
        tail = _tail_chars(run.input.get("tail_chars"))
        task = await self._client(context).get(task_id, tail_chars=tail)
        return _row(task, tail=tail > 0)

    async def _list(self, run: RunContext[PackContext]) -> dict[str, Any]:
        tasks = await self._client(run.ctx).tasks()
        return {
            "tasks": [_row(task) for task in tasks],
            "count": len(tasks),
        }

    async def _cancel(self, run: RunContext[PackContext]) -> dict[str, Any]:
        context = run.ctx
        task_id = str(run.input.get("task") or "").strip()
        if not task_id:
            return {"status": "invalid", "message": "name the task to stop"}
        task = await self._client(context).cancel(task_id)
        return _row(task)

    # ------------------------------------------------------------------ tracking

    async def _tracked(self, context: PackContext, task: CoderTask) -> dict[str, Any]:
        """The task as a result, with a work item watching it when the registry is here."""
        registry = context.work
        if registry is None or not task.live:
            return _row(task)
        client = self._client(context)
        tags = await waking_tags(
            context.subscriptions,
            quiet=context.policy.quiet,
            wake=True,
            timeout_seconds=TASK_SECONDS,
        )
        work_id = new_id()
        handle = registry.start(
            _settled(client, registry, task.id, work_id),
            Brief(
                session_id=context.session_id,
                kind=Kind.job,
                role="coder",
                objective=task.title or task.brief[:160],
                timeout_seconds=TASK_SECONDS,
                account_id=context.account_id,
                wake=True,
                tags=tags,
            ),
            work_id=work_id,
        )
        row = _row(task)
        row["work_id"] = handle.id
        row["notice"] = STARTED.format(state=task.state, work_id=handle.id)
        return row

    def _client(self, context: PackContext) -> CoderClient:
        if self._override is not None:
            return self._override
        return HttpCoderClient(context.http, self.base_url, audience=self.audience)


def _switched_off(base_url: str, context: PackContext) -> Availability | None:
    """The operator's and the person's switches, before anything is asked of the bridge."""
    if not base_url:
        return Availability(state=State.not_configured, detail="no Claude Code bridge is deployed")
    if not context.policy.claude_code_delegation:
        return Availability(state=State.disabled, detail=PERSONS_SWITCH)
    if not context.policy.claude_code_directories:
        return Availability(state=State.disabled, detail=NO_DIRECTORIES)
    return None


async def _settled(
    client: CoderClient, registry: Registry, task_id: str, work_id: str
) -> dict[str, Any]:
    """Poll the bridge until the task leaves its live states; the final row is the payload.

    Progress is hub-written sentences from counters -- "14 tool uses, last: Edit" -- never
    Claude Code's own text, because `progress` reaches the live block unfenced.
    """
    missed = 0
    while True:
        try:
            task = await client.get(task_id)
        except (DownstreamError, TransportError) as exc:
            # A bridge restart takes seconds; the task itself keeps running on the host.
            # Give up only after a stretch of silence, and say where the task still is.
            missed += 1
            if missed >= POLL_MISSES:
                raise WorkError(BRIDGE_SILENT.format(task_id=task_id)) from exc
            await asyncio.sleep(POLL_SECONDS)
            continue
        missed = 0
        if not task.live:
            return _row(task)
        if task.tool_uses:
            registry.progress(
                work_id, f"{task.tool_uses} tool uses, last: {task.last_tool or 'unknown'}"
            )
        await asyncio.sleep(POLL_SECONDS)


def _row(task: CoderTask, *, tail: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task": task.id,
        "title": task.title,
        "state": task.state,
        "directory": task.directory,
        "run_level": task.run_level,
        "turns": task.turns,
        "cost_usd": task.cost_usd,
        "tool_uses": task.tool_uses,
        "last_tool": task.last_tool,
        "resumable": task.resumable,
    }
    if task.detail:
        row["detail"] = task.detail
    if task.result:
        row["result"] = task.result
    if task.model:
        row["model"] = task.model
    if task.permission_denials:
        row["permission_denials"] = list(task.permission_denials)
    advice = _denial_advice(task.permission_denials) or task.advice
    if advice:
        row["advice"] = advice
    if tail and task.transcript_tail:
        row["transcript_tail"] = task.transcript_tail
    return row


def _denial_advice(denials: tuple[dict[str, str], ...]) -> str:
    """The hub's sentence about what Claude Code was refused, and how a yes lets it.

    Only the tool names reach it -- never the refused input, which is the program's words
    -- and the rules are bare names: a person's yes to "Bash" in general is theirs to narrow
    if they want, by saying so.
    """
    tools = sorted({denial["tool"] for denial in denials if denial.get("tool")})
    if not tools:
        return ""
    rules = "[" + ", ".join(f'"{tool}"' for tool in tools) + "]"
    return REFUSED_TOOLS.format(count=len(denials), tools=", ".join(tools), rules=rules)


def _within(mode: str, ceiling: str) -> bool:
    """Whether `mode` is the person's level or more careful. An unknown mode never is."""
    if mode not in MODES or ceiling not in MODES:
        return False
    return MODES.index(mode) <= MODES.index(ceiling)


def _allowed(directory: str, listed: tuple[str, ...]) -> str | None:
    """The listed folder this one is, or ``None``.

    Exact membership, compared case-insensitively with separators and trailing slashes
    normalised -- never a prefix match, because allowing `C:/repos` must not allow
    `C:/repos-secret`, and never resolution against the hub's own disk, which is a
    container that cannot see the host's.
    """
    wanted = _normal(directory)
    if not wanted:
        return None
    for allowed in listed:
        if _normal(allowed) == wanted:
            return allowed
    return None


def _normal(path: str) -> str:
    return path.strip().replace("\\", "/").rstrip("/").casefold()


def _tail_chars(given: object) -> int:
    try:
        return max(0, int(str(given)))
    except (TypeError, ValueError):
        return 0
