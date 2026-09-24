"""Helpers the model can start, check in on, and steer.

A helper is work: it gets a handle, a notice, and a fetch, the same as a download. Starting
records a typed brief and runs a child loop with a clean context. The child's return is
capped; the rest lives on the child's own items, which the parent does not see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.agents.types import CONTINUABLE, STOPPED
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.prompt.docs import capability_doc
from lucy_api.work.registry import AtCapacityError, Registry
from lucy_api.work.registry import _discard as discard_unstarted
from lucy_api.work.types import Brief, Handle, Kind, WorkError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.packs.context import PackContext

MAX_DEPTH = 3
"""How deep helpers may nest. Four levels is a system nobody can follow, including Lucy."""


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

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:
        if context.work is None:
            return Availability(state=State.not_configured, detail="no work registry this turn")
        running = _helpers_running(context.work, context.session_id)
        detail = f"{running} helpers running" if running else "no helpers running"
        return Availability(state=State.ready, detail=detail)

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        registry, session_id, depth = context.work, context.session_id, context.depth
        if registry is None:
            return ()

        async def run_list(_run: RunContext[Any]) -> dict[str, Any]:
            return _list(registry, session_id)

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
                    "Helpers currently running for this conversation. Finished ones "
                    "arrive as work.check notices, not here (helpers, subagents, roster)."
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
        shared = (listed, message, journal_read, journal_claim, journal_complete)
        if context.agent_id:
            return shared
        return (
            shared[0],
            define_operation(
                {
                    "name": "agents.spawn",
                    "description": (
                        "Start a helper with a brief saying what it is for. It returns a "
                        "handle immediately; read the result with work.result when the "
                        "notice arrives (delegate, helper, subagent, spawn)."
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


def _helpers_running(registry: Registry, session_id: str) -> int:
    return sum(1 for record in registry.running(session_id) if record.kind is Kind.helper)


def _at_helper_cap(registry: Registry, context: PackContext) -> dict[str, Any] | None:
    helpers = _helpers_running(registry, context.session_id)
    if helpers < context.policy.agent_max_concurrent:
        return None
    return {
        "status": "at_capacity",
        "message": (
            f"helper cap reached ({helpers} running, "
            f"limit {context.policy.agent_max_concurrent}); "
            "wait for one to finish or cancel one"
        ),
    }


async def _begin_helper(  # noqa: PLR0913 - start plus the setup to discard if the cap is hit
    registry: Registry,
    context: PackContext,
    *,
    runtime: Any,
    work: Any,
    brief: Brief,
    work_id: str,
    discard: tuple[str, int],
) -> Handle | dict[str, Any]:
    refused = _at_helper_cap(registry, context)
    if refused is not None:
        discard_unstarted(work)
        await runtime.discard_setup(context, discard[0], discard[1])
        return refused
    try:
        return registry.start(work, brief, work_id=work_id)
    except AtCapacityError as exc:
        await runtime.discard_setup(context, discard[0], discard[1])
        return {"status": "at_capacity", "message": str(exc)}


def _list(registry: Registry, session_id: str) -> dict[str, Any]:
    running = [record for record in registry.running(session_id) if record.kind is Kind.helper]
    return {
        "running": [
            {
                "id": record.id,
                "role": record.role,
                "objective": record.objective,
                "depth": record.depth,
                "progress": record.progress or record.detail,
            }
            for record in running
        ],
        "count": len(running),
    }


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
        return {
            "status": "not_configured",
            "message": "helpers cannot run in this turn; the child runtime is not attached",
        }
    name = role.strip() or "helper"
    cap = context.max_subagent_turns
    refused = _at_helper_cap(registry, context)
    if refused is not None:
        return refused

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

    try:
        handle = registry.start(
            work(),
            Brief(
                session_id=context.session_id,
                kind=Kind.helper,
                role=name,
                objective=brief,
                depth=depth + 1,
                timeout_seconds=float(max(1, cap) * 30),
                account_id=context.account_id,
                # The main thread's helpers wake an idle session when they finish; a
                # helper's helpers do not, because their parent is still running and is
                # the one that will read them.
                wake=depth == 0,
            ),
            work_id=agent_id,
        )
    except AtCapacityError as exc:
        await runtime.discard_setup(context, agent_id, task_id)
        return {"status": "at_capacity", "message": str(exc)}
    return {
        "id": handle.id,
        "role": handle.role,
        "state": "running",
        "advice": "It is running. A notice arrives when it finishes; then read work.result.",
    }


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
        return {
            "status": "not_configured",
            "message": "helpers cannot run in this turn; the child runtime is not attached",
        }
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
        work=work(),
        brief=Brief(
            session_id=context.session_id,
            kind=Kind.helper,
            role=role,
            objective=objective,
            depth=depth + 1,
            timeout_seconds=float(max(1, context.max_subagent_turns) * 30),
            account_id=context.account_id,
            wake=depth == 0,
        ),
        work_id=new_id,
        discard=(new_id, task_id),
    )
    if isinstance(started, dict):
        return started
    return {
        "id": started.id,
        "role": started.role,
        "state": "running",
        "resume_from": handle,
        "advice": "It is running from the previous transcript. Read work.result when it finishes.",
    }


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
        return {
            "status": "not_configured",
            "message": "helpers cannot run in this turn; the child runtime is not attached",
        }
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
        return {
            "status": "not_configured",
            "message": "helpers cannot run in this turn; the child runtime is not attached",
        }
    return await runtime.read_journal(context)


async def _journal_claim(context: PackContext, task_id: str) -> dict[str, Any]:
    runtime = context.child
    if runtime is None:
        return {
            "status": "not_configured",
            "message": "helpers cannot run in this turn; the child runtime is not attached",
        }
    handle = task_id.strip()
    if not handle:
        return {"status": "invalid", "message": "name the task by the id journal.read returned"}
    return await runtime.claim(context, handle)


async def _journal_complete(context: PackContext, task_id: str) -> dict[str, Any]:
    runtime = context.child
    if runtime is None:
        return {
            "status": "not_configured",
            "message": "helpers cannot run in this turn; the child runtime is not attached",
        }
    handle = task_id.strip()
    if not handle:
        return {"status": "invalid", "message": "name the task by the id journal.read returned"}
    return await runtime.complete(context, handle)


__all__ = ["MAX_DEPTH", "AgentsPack"]
