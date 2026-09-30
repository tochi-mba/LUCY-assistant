"""Helpers the model can start, check in on, and steer.

A helper is work: it gets a handle, a notice, and a fetch, the same as a download. Starting
records a typed brief and runs a child loop with a clean context. The child's return is
capped; the rest lives on the child's own items, which the parent does not see.

A spawn past the person's cap (`agent_max_concurrent`) is queued rather than refused: it
gets its handle at once, waits in order, and starts on its own when one of this
conversation's helpers ends. So a team larger than the cap is started in one plan and
staged by the hub, not by a model counting slots. The queue holds as many as the cap; only
past that is a spawn refused, as a result the model can act on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.agents.types import CONTINUABLE, STOPPED
from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Permission, SetupPlan
from lucy_api.packs.base import State as PackState
from lucy_api.prompt.docs import capability_doc
from lucy_api.work.registry import AtCapacityError, Registry
from lucy_api.work.types import Brief, Kind, State, WorkError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.packs.context import PackContext

MAX_DEPTH = 3
"""How deep helpers may nest. Four levels is a system nobody can follow, including Lucy."""

MAX_STOPPED_LISTED = 10
"""How many stopped helpers `agents.list` shows, newest last. `stopped_count` says how many."""

WALL_CLOCK_GRACE = 30.0
"""How far past its own wall clock the registry lets a helper run.

The runtime enforces `agent_wall_clock_seconds` itself, and a helper stopped that way ends as
"ran out of time", continuable, with its roster row and journal task written. The registry's
deadline is only the backstop behind it. It used to be eight rounds times thirty seconds, so it
fired first -- at four minutes, against a setting that promises ten and allows two hours -- and
cancelled the helper, which the roster then recorded as cancelled by somebody.
"""


WRITTEN_BY_HELPERS = frozenset({"agents.read"})
"""Operations that hand back a helper's own words and what it read."""


class AgentsPack:
    """Start a helper, list the ones running, or send one a mid-run steer."""

    id = "agents"
    title = "Helpers"
    summary = "Ask a helper to take a brief and come back with a short result."

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="agents.delegate",
                title="Start a helper",
                description="Spin up a helper on this conversation with a written brief.",
                risk="write",
                covers=("agents.spawn", "agents.reopen", "journal.claim", "journal.complete"),
            ),
        )

    def result_trust(self, operation: str, data: object) -> Trust:
        """Lucy's own roster and journal, except what a helper itself wrote."""
        del data
        return Trust.untrusted if operation in WRITTEN_BY_HELPERS else Trust.observed

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:
        if context.work is None:
            return Availability(state=PackState.not_configured, detail="no work registry this turn")
        running, queued = _helpers_in_flight(context.work, context.session_id)
        detail = f"{running} helpers running" if running else "no helpers running"
        if queued:
            detail += f", {queued} queued"
        return Availability(state=PackState.ready, detail=detail)

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        registry, depth = context.work, context.depth
        if registry is None:
            return ()

        async def run_list(_run: RunContext[Any]) -> dict[str, Any]:
            return await _list(registry, context)

        async def run_spawn(run: RunContext[Any]) -> dict[str, Any]:
            return await _spawn(
                registry,
                context,
                depth=depth,
                objective=str(run.input.get("objective") or ""),
                role=str(run.input.get("role") or "helper"),
                return_schema=str(run.input.get("return_schema") or ""),
            )

        async def run_message(run: RunContext[Any]) -> dict[str, Any]:
            return await _message(
                context,
                agent_id=str(run.input.get("id") or ""),
                body=str(run.input.get("message") or ""),
            )

        async def run_reopen(run: RunContext[Any]) -> dict[str, Any]:
            return await _reopen(
                registry,
                context,
                depth=depth,
                agent_id=str(run.input.get("id") or ""),
                return_schema=str(run.input.get("return_schema") or ""),
            )

        async def run_read(run: RunContext[Any]) -> dict[str, Any]:
            return await _read(context, str(run.input.get("id") or ""))

        async def run_journal_read(_run: RunContext[Any]) -> dict[str, Any]:
            return await _journal_read(context)

        async def run_journal_claim(run: RunContext[Any]) -> dict[str, Any]:
            return await _journal_claim(context, str(run.input.get("id") or ""))

        async def run_journal_complete(run: RunContext[Any]) -> dict[str, Any]:
            return await _journal_complete(context, str(run.input.get("id") or ""))

        listed = define_operation(
            {
                "name": "agents.list",
                "description": (
                    "Helpers running for this conversation, and any that stopped before "
                    "finishing and were not continued. Finished ones arrive as work.check "
                    "notices, not here (helpers, subagents, roster, stopped, resume)."
                ),
                "input": object_schema({}),
                "output": value(object_schema({})),
                "effects": "read",
                "run": run_list,
            }
        )
        message = define_operation(
            {
                "name": "agents.message",
                "description": (
                    "Steer a running helper. The message is delivered at the next tool "
                    "boundary, never mid-tool (delegate, helper, inbox, steer)."
                ),
                "input": object_schema(
                    {
                        "id": string_schema().describe("The helper's handle."),
                        "message": string_schema().describe(
                            "What it should do next, as a sentence."
                        ),
                    }
                ),
                "output": value(object_schema({})),
                "effects": "read",
                "run": run_message,
            }
        )
        read = define_operation(
            {
                "name": "agents.read",
                "description": (
                    "What a helper has done so far, in order: its brief, each step and what "
                    "came back, and what it said. Works while it runs and after it "
                    "finished, stopped or was cancelled, and changes nothing (transcript, "
                    "progress, partial, inspect)."
                ),
                "input": object_schema({"id": string_schema().describe("The helper's handle.")}),
                "output": value(object_schema({})),
                "effects": "read",
                "run": run_read,
            }
        )
        journal_read = define_operation(
            {
                "name": "journal.read",
                "description": (
                    "The blackboard of tasks for this conversation: who claimed what, "
                    "what is blocked, what is still open (journal, tasks, helpers)."
                ),
                "input": object_schema({}),
                "output": value(object_schema({})),
                "effects": "read",
                "run": run_journal_read,
            }
        )
        journal_claim = define_operation(
            {
                "name": "journal.claim",
                "description": (
                    "Claim one open journal task so siblings can see who is doing it. "
                    "A dead claim expires on its own (journal, lease, helper)."
                ),
                "input": object_schema(
                    {"id": string_schema().describe("The task id journal.read returned.")}
                ),
                "output": value(object_schema({})),
                "effects": "write",
                "run": run_journal_claim,
            }
        )
        journal_complete = define_operation(
            {
                "name": "journal.complete",
                "description": (
                    "Mark a journal task finished. Other helpers waiting on it can then "
                    "proceed (journal, complete, helper)."
                ),
                "input": object_schema(
                    {"id": string_schema().describe("The task id journal.read returned.")}
                ),
                "output": value(object_schema({})),
                "effects": "write",
                "run": run_journal_complete,
            }
        )
        shared = (listed, read, message, journal_read, journal_claim, journal_complete)
        if context.agent_id:
            return shared
        return (
            shared[0],
            define_operation(
                {
                    "name": "agents.spawn",
                    "description": (
                        "Start a helper with a brief saying what it is for. It returns a "
                        "handle immediately, running or queued behind the cap; read the "
                        "result with work.result when the notice arrives (delegate, helper, "
                        "subagent, spawn, team)."
                    ),
                    "input": object_schema(
                        {
                            "objective": string_schema().describe(
                                "What the helper is for, as a sentence, not an argument list."
                            ),
                            "role": string_schema()
                            .optional()
                            .describe("A short name, like reviewer or researcher."),
                            "return_schema": string_schema()
                            .optional()
                            .describe(
                                "JSON Schema the helper must return as its whole answer, "
                                "so you get an object rather than prose."
                            ),
                        }
                    ),
                    "output": value(object_schema({"id": string_schema()})),
                    "effects": "write",
                    "run": run_spawn,
                }
            ),
            define_operation(
                {
                    "name": "agents.reopen",
                    "description": (
                        "Continue a finished helper from its transcript. The new run sees "
                        "the previous items and last report; it does not revive the old "
                        "process (reopen, helper, resume)."
                    ),
                    "input": object_schema(
                        {
                            "id": string_schema().describe(
                                "The finished helper's handle from agents.spawn."
                            ),
                            "return_schema": string_schema()
                            .optional()
                            .describe("JSON Schema for this run's return, if you want one."),
                        }
                    ),
                    "output": value(object_schema({"id": string_schema()})),
                    "effects": "write",
                    "run": run_reopen,
                }
            ),
            *shared[1:],
        )


def _deadline(context: PackContext) -> float:
    """The registry's backstop: the helper's own wall clock, and a grace to end by it."""
    return float(context.policy.agent_wall_clock_seconds) + WALL_CLOCK_GRACE


def _helpers_in_flight(registry: Registry, session_id: str) -> tuple[int, int]:
    """How many of this conversation's helpers are running, and how many wait to start."""
    helpers = [record for record in registry.running(session_id) if record.kind is Kind.helper]
    queued = sum(1 for record in helpers if record.state is State.queued)
    return len(helpers) - queued, queued


async def _begin_helper(  # noqa: PLR0913 - start plus the setup to discard if the queue is full
    registry: Registry,
    context: PackContext,
    *,
    runtime: Any,
    work: Callable[[], Awaitable[object]],
    brief: Brief,
    work_id: str,
    discard: tuple[str, int],
    advice: str,
) -> dict[str, Any]:
    """Start a prepared helper, or queue it behind the cap, or refuse it past the queue.

    `advice` is what a helper that started at once is handed back with.

    The cap is the person's `agent_max_concurrent`, and the queue holds as many again: a
    team of twice the cap can be started in one plan. A helper refused past that has its
    prepared roster row and journal task removed, so nothing is left that never ran.
    """
    cap = context.policy.agent_max_concurrent

    async def withdraw() -> None:
        await runtime.withdraw(context, discard[0], discard[1])

    try:
        handle = registry.queue(
            work, brief, slots=cap, waiting=cap, work_id=work_id, dropped=withdraw
        )
    except AtCapacityError as exc:
        await runtime.discard_setup(context, discard[0], discard[1])
        return {"status": "at_capacity", "message": f"helper queue full: {exc}"}
    if registry.state_of(handle.id) is State.queued:
        running, queued = _helpers_in_flight(registry, context.session_id)
        return {
            "id": handle.id,
            "role": handle.role,
            "state": "queued",
            "advice": (
                f"{running} helpers are running, as many as the person allows at once, and "
                f"this is {queued} in the queue. It starts on its own when one of them "
                "finishes, and its time starts then; work.cancel takes it out of the queue."
            ),
        }
    return {"id": handle.id, "role": handle.role, "state": "running", "advice": advice}


def _not_attached() -> dict[str, Any]:
    """The answer every helper operation gives in a turn that has no helper runtime."""
    return {
        "status": "not_configured",
        "message": "helpers cannot run in this turn; the child runtime is not attached",
    }


async def _read(context: PackContext, agent_id: str) -> dict[str, Any]:
    if context.child is None:
        return _not_attached()
    return await context.child.transcript(context, agent_id)


async def _list(registry: Registry, context: PackContext) -> dict[str, Any]:
    running = [
        record for record in registry.running(context.session_id) if record.kind is Kind.helper
    ]
    stopped = await context.child.stopped(context) if context.child is not None else []
    listed: dict[str, Any] = {
        "running": [
            {
                "id": record.id,
                "role": record.role,
                "objective": record.objective,
                "depth": record.depth,
                "state": record.state.value,
                "progress": record.progress or record.detail,
            }
            for record in running
        ],
        "count": len(running),
    }
    queued = sum(1 for record in running if record.state is State.queued)
    if queued:
        listed["queued"] = queued
    if stopped:
        listed["stopped"] = stopped[-MAX_STOPPED_LISTED:]
        listed["stopped_count"] = len(stopped)
        listed["advice"] = (
            "agents.read shows what a stopped helper did; agents.reopen continues it from "
            "its own transcript."
        )
    return listed


async def _spawn(  # noqa: PLR0913 - spawn is the brief plus the depth the parent already has
    registry: Registry,
    context: PackContext,
    *,
    depth: int,
    objective: str,
    role: str,
    return_schema: str = "",
) -> dict[str, Any]:
    brief = objective.strip()
    if not brief:
        return {"status": "invalid", "message": "say what the helper is for, in a sentence"}
    if depth >= context.policy.agent_max_depth:
        return {
            "status": "too_deep",
            "message": (f"helpers may nest {context.policy.agent_max_depth} deep, not {depth + 1}"),
        }
    runtime = context.child
    if runtime is None:
        return _not_attached()
    name = role.strip() or "helper"

    agent_id, task_id = await runtime.prepare(
        context, objective=brief, role=name, return_schema=return_schema
    )

    async def work() -> dict[str, Any]:
        return _ended(
            await runtime.run(
                context,
                objective=brief,
                role=name,
                agent_id=agent_id,
                task_id=task_id,
            )
        )

    return await _begin_helper(
        registry,
        context,
        runtime=runtime,
        work=work,
        brief=Brief(
            session_id=context.session_id,
            kind=Kind.helper,
            role=name,
            objective=brief,
            depth=depth + 1,
            timeout_seconds=_deadline(context),
            account_id=context.account_id,
            # The main thread's helpers wake an idle session when they finish; a helper's
            # helpers do not, because their parent is still running and is the one that
            # will read them.
            wake=depth == 0,
        ),
        work_id=agent_id,
        discard=(agent_id, task_id),
        advice=(
            "It is running. A notice arrives when it finishes; then read work.result. "
            "agents.read shows what it has done so far, and work.cancel stops it."
        ),
    )


async def _reopen(
    registry: Registry,
    context: PackContext,
    *,
    depth: int,
    agent_id: str,
    return_schema: str = "",
) -> dict[str, Any]:
    handle = agent_id.strip()
    if not handle:
        return {
            "status": "invalid",
            "message": "name the finished helper by the handle agents.spawn returned",
        }
    if depth >= context.policy.agent_max_depth:
        return {
            "status": "too_deep",
            "message": (f"helpers may nest {context.policy.agent_max_depth} deep, not {depth + 1}"),
        }
    runtime = context.child
    if runtime is None:
        return _not_attached()
    prepared = await runtime.reopen(context, handle, return_schema=return_schema)
    new_id = str(prepared.get("agent_id") or "")
    if not new_id:
        return prepared
    task_id = int(prepared.get("task_id") or 0)
    objective = str(prepared.get("objective") or handle)
    role = str(prepared.get("role") or "helper")

    async def work() -> dict[str, Any]:
        return _ended(
            await runtime.run(
                context,
                objective=objective,
                role=role,
                agent_id=new_id,
                task_id=task_id,
            )
        )

    started = await _begin_helper(
        registry,
        context,
        runtime=runtime,
        work=work,
        brief=Brief(
            session_id=context.session_id,
            kind=Kind.helper,
            role=role,
            objective=objective,
            depth=depth + 1,
            timeout_seconds=_deadline(context),
            account_id=context.account_id,
            wake=depth == 0,
        ),
        work_id=new_id,
        discard=(new_id, task_id),
        advice="It is running from the previous transcript. Read work.result when it finishes.",
    )
    return {**started, "resume_from": handle} if "id" in started else started


def _ended(result: dict[str, Any]) -> dict[str, Any]:
    """A helper's return when it finished, and a failed ending that says why when it did not.

    The runtime answers a helper that stopped partway -- its model unavailable, out of
    rounds, out of time -- with a result rather than an exception, so its report of how far
    it got survives. Handed to the registry as it was, that result was recorded as a
    success: the notice said `succeeded`, and a model had to read the payload to learn the
    helper had died, which on the strength of that notice it had no reason to do.
    """
    if result.get("status") == "ok":
        return result
    lead = CONTINUABLE if result.get("resumable") else STOPPED
    why = str(result.get("summary") or "").strip()
    raise WorkError(f"{lead}: {why}" if why else lead, payload=result)


async def _message(context: PackContext, *, agent_id: str, body: str) -> dict[str, Any]:
    runtime = context.child
    if runtime is None:
        return _not_attached()
    handle = agent_id.strip()
    if not handle:
        return {
            "status": "invalid",
            "message": "name the helper by the handle agents.spawn returned",
        }
    return await runtime.send(context, handle, body)


async def _journal_read(context: PackContext) -> dict[str, Any]:
    runtime = context.child
    if runtime is None:
        return _not_attached()
    return await runtime.read_journal(context)


async def _journal_claim(context: PackContext, task_id: str) -> dict[str, Any]:
    runtime = context.child
    if runtime is None:
        return _not_attached()
    handle = task_id.strip()
    if not handle:
        return {"status": "invalid", "message": "name the task by the id journal.read returned"}
    return await runtime.claim(context, handle)


async def _journal_complete(context: PackContext, task_id: str) -> dict[str, Any]:
    runtime = context.child
    if runtime is None:
        return _not_attached()
    handle = task_id.strip()
    if not handle:
        return {"status": "invalid", "message": "name the task by the id journal.read returned"}
    return await runtime.complete(context, handle)


__all__ = ["MAX_DEPTH", "MAX_STOPPED_LISTED", "WALL_CLOCK_GRACE", "AgentsPack"]
