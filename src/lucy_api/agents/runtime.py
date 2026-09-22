"""A helper is a child run of the same loop, with a clean context and a return cap.

The pack starts the work; this module is what actually runs. Keeping the loop out of the
pack is the layering: packs describe capabilities, the runtime owns the model, and the
brief is the only thing that crosses.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from lucy_api.agents.types import Delegation, capped_summary, declared_return
from lucy_api.core.errors import LucyError
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.sessions.sql_store import NewItem
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.prompt import SessionView, system_and_messages, view_limits
from lucy_api.turn.stop import Budget, Termination

if TYPE_CHECKING:
    from lucy_api.agents.store import AgentStore
    from lucy_api.model.registry import ModelRegistry
    from lucy_api.model.types import Message
    from lucy_api.packs.context import PackContext
    from lucy_api.packs.service import Capabilities
    from lucy_api.sessions.sql_store import SessionStore


def _brief_text(delegation: Delegation) -> str:
    lines = [
        f"You are a helper named {delegation.role}.",
        f"Objective: {delegation.objective}",
        f"Return: {delegation.output_format}",
        "You are read-only. Do not write files, change settings, or start more helpers.",
    ]
    if delegation.constraints:
        lines.append(f"Constraints: {delegation.constraints}")
    if delegation.boundaries:
        lines.append(f"Boundaries: {delegation.boundaries}")
    if delegation.guidance:
        lines.append(f"Guidance: {delegation.guidance}")
    if delegation.return_schema:
        lines.append(
            "Return JSON matching this schema as your whole answer, no markdown: "
            + delegation.return_schema
        )
    if delegation.resume_from:
        lines.append(
            f"You are continuing helper {delegation.resume_from}. Its earlier items "
            "are in this transcript; do not repeat finished work."
        )
    lines.append("Messages from the parent arrive as notices before a round, never mid-tool.")
    return "\n".join(lines)


class ChildRuntime:
    """Runs one helper to completion against the parent's capabilities and model."""

    def __init__(
        self,
        store: SessionStore,
        agents: AgentStore,
        models: ModelRegistry,
        capabilities: Capabilities,
    ) -> None:
        self.store = store
        self.agents = agents
        self.models = models
        self.capabilities = capabilities
        self._wait = asyncio.wait_for

    async def prepare(  # noqa: PLR0913 - the brief is objective, role, resume and schema
        self,
        parent: PackContext,
        *,
        objective: str,
        role: str,
        resume_from: str = "",
        return_schema: str = "",
        guidance: str = "",
    ) -> tuple[str, int]:
        """Persist a helper before its public handle can be returned."""
        delegation = Delegation(
            objective=objective.strip(),
            role=role.strip() or "helper",
            max_iterations=max(1, parent.max_subagent_turns),
            resume_from=resume_from.strip(),
            return_schema=return_schema.strip(),
            guidance=guidance.strip(),
        )
        agent_id = await self.agents.insert(
            parent.account_id,
            parent.session_id,
            role=delegation.role,
            objective=delegation.objective,
            depth=parent.depth + 1,
            parent_agent_id=parent.agent_id or None,
            delegation={
                "objective": delegation.objective,
                "role": delegation.role,
                "output_format": delegation.output_format,
                "guidance": delegation.guidance,
                "resume_from": delegation.resume_from,
                "return_schema": delegation.return_schema,
            },
        )
        task_id = await self.agents.add_task(
            parent.account_id,
            parent.session_id,
            title=delegation.objective,
            agent_id=agent_id,
        )
        return agent_id, task_id

    async def run(
        self,
        parent: PackContext,
        *,
        objective: str,
        role: str,
        agent_id: str = "",
        task_id: int = 0,
    ) -> dict[str, Any]:
        """Run, cap and persist a prepared helper; direct callers may prepare implicitly."""
        stored: dict[str, Any] = {}
        if agent_id:
            row = await self.agents.get(parent.account_id, agent_id)
            payload = row.get("delegation")
            if isinstance(payload, dict):
                stored = payload
        delegation = Delegation(
            objective=str(stored.get("objective") or objective).strip(),
            role=str(stored.get("role") or role).strip() or "helper",
            max_iterations=max(1, parent.max_subagent_turns),
            guidance=str(stored.get("guidance") or ""),
            resume_from=str(stored.get("resume_from") or ""),
            return_schema=str(stored.get("return_schema") or ""),
            output_format=str(
                stored.get("output_format")
                or "a short summary with references, never a raw transcript"
            ),
        )
        if not agent_id or not task_id:
            agent_id, task_id = await self.prepare(
                parent, objective=delegation.objective, role=delegation.role
            )
        try:
            # Do not write the brief until Registry has accepted the helper.  If the
            # per-session capacity check rejects it, discard_setup can then remove the
            # prepared rows without leaving an orphaned transcript item behind.
            await self.store.append(
                parent.account_id,
                parent.session_id,
                NewItem("message", "user", _brief_text(delegation), agent_id=agent_id),
            )
            result = await self._wait(
                self._loop(parent, agent_id, delegation),
                timeout=parent.policy.agent_wall_clock_seconds,
            )
        except asyncio.CancelledError:
            await self.agents.finish(
                parent.account_id,
                agent_id,
                status="interrupted",
                interrupted_reason="cancelled",
            )
            await self.agents.finish_task(
                parent.account_id, parent.session_id, task_id, status="cancelled"
            )
            raise
        except TimeoutError:
            result = {
                "status": "failed",
                "agent_id": agent_id,
                "role": delegation.role,
                "summary": "the helper was stopped because it ran out of time",
                "tokens": 0,
            }
        except Exception as exc:
            result = {
                "status": "failed",
                "agent_id": agent_id,
                "role": delegation.role,
                "summary": f"the helper stopped ({type(exc).__name__})",
                "tokens": 0,
            }
        finished = "completed" if result["status"] == "ok" else "failed"
        await self.agents.finish(
            parent.account_id,
            agent_id,
            status=finished,
            result=result,
            summary_tokens=int(result.get("tokens") or 0),
        )
        await self.agents.finish_task(
            parent.account_id,
            parent.session_id,
            task_id,
            status="completed" if finished == "completed" else "failed",
        )
        return result

    async def discard_setup(self, parent: PackContext, agent_id: str, task_id: int) -> None:
        """Compensate when the in-memory registry cannot accept a prepared helper."""
        await self.agents.discard_setup(parent.account_id, parent.session_id, agent_id, task_id)

    async def send(self, parent: PackContext, agent_id: str, body: str) -> dict[str, Any]:
        """Deliver a parent message to a running helper's inbox."""
        row = await self.agents.get(parent.account_id, agent_id)
        if row["status"] != "running":
            return {
                "status": "finished",
                "message": "that helper has finished; read work.result or start another",
            }
        text = body.strip()
        if not text:
            return {"status": "invalid", "message": "say what the helper should do next"}
        try:
            await self.agents.send_mail(
                parent.account_id,
                agent_id,
                text,
                max_chars=parent.policy.agent_message_max_chars,
                burst=parent.policy.agent_message_burst,
            )
        except LucyError as exc:
            return {"status": exc.code, "message": str(exc)}
        return {"status": "delivered", "id": agent_id}

    async def reopen(
        self, parent: PackContext, agent_id: str, *, return_schema: str = ""
    ) -> dict[str, Any]:
        """Start a new helper that continues a finished one's transcript."""
        try:
            row = await self.agents.get(parent.account_id, agent_id)
        except LucyError as exc:
            return {"status": exc.code, "message": str(exc)}
        if row["status"] == "running":
            return {
                "status": "running",
                "message": "that helper is still running; steer it with agents.message",
            }
        result = row.get("result")
        summary = ""
        if isinstance(result, dict):
            summary = str(result.get("summary") or "")
        guidance = f"Continue from helper {agent_id}."
        if summary:
            guidance += f" Its last report was:\n{summary}"
        schema = return_schema.strip()
        stored = row.get("delegation")
        if not schema and isinstance(stored, dict):
            schema = str(stored.get("return_schema") or "")
        new_id, task_id = await self.prepare(
            parent,
            objective=str(row["objective"]),
            role=str(row["role"]),
            resume_from=agent_id,
            return_schema=schema,
            guidance=guidance,
        )
        return {
            "agent_id": new_id,
            "task_id": task_id,
            "resume_from": agent_id,
            "objective": str(row["objective"]),
            "role": str(row["role"]),
        }

    async def read_journal(self, parent: PackContext) -> dict[str, Any]:
        tasks = await self.agents.tasks(parent.account_id, parent.session_id)
        return {
            "tasks": [
                {
                    "id": item.id,
                    "title": item.title,
                    "status": item.status,
                    "claimed_by": item.claimed_by,
                    "blocked_by": list(item.blocked_by),
                }
                for item in tasks
            ]
        }

    async def claim(self, parent: PackContext, task_id: str) -> dict[str, Any]:
        claimant = parent.agent_id or "parent"
        try:
            number = int(task_id)
        except ValueError:
            return {"status": "invalid", "message": "name the task by the id journal.read returned"}
        try:
            await self.agents.claim_task(
                parent.account_id, parent.session_id, number, agent_id=claimant
            )
        except LucyError as exc:
            return {"status": exc.code, "message": str(exc)}
        return {"status": "claimed", "id": str(number), "claimed_by": claimant}

    async def complete(self, parent: PackContext, task_id: str) -> dict[str, Any]:
        try:
            number = int(task_id)
        except ValueError:
            return {"status": "invalid", "message": "name the task by the id journal.read returned"}
        try:
            await self.agents.complete_task(parent.account_id, parent.session_id, number)
        except LucyError as exc:
            return {"status": exc.code, "message": str(exc)}
        return {"status": "completed", "id": str(number)}

    async def _loop(
        self, parent: PackContext, agent_id: str, delegation: Delegation
    ) -> dict[str, Any]:
        session = await self.store.get(parent.account_id, parent.session_id)
        workspace = None
        if parent.workspace_environment_id:
            workspace = WorkspaceScope(parent.workspace_environment_id, parent.session_id)
        scope = SessionScope(
            account_id=parent.account_id,
            profile=parent.profile,
            session_id=parent.session_id,
            turn_id=parent.turn_id,
            permission_mode=parent.permission_mode,
            incognito=parent.incognito,
            workspace=workspace,
        ).for_agent(agent_id, permission_mode="plan")
        child = self.capabilities.context_for(scope, http=parent.http, tokens=parent.tokens)
        child.grants = dict(parent.grants)
        child.policy = parent.policy
        child.max_subagent_turns = delegation.max_iterations
        child.work = parent.work
        catalogue = await self.capabilities.probe(child)
        provider = self.models.resolve(str(session["model"]))

        async def assemble(notice: str) -> tuple[str, tuple[Message, ...]]:
            mail = await self.agents.drain_mail(parent.account_id, agent_id)
            extra = ""
            if mail:
                extra = "Messages from the parent:\n" + "\n".join(f"- {line}" for line in mail)
            combined = "\n".join(part for part in (notice, extra) if part)
            rows = await self.store.records(parent.account_id, parent.session_id, "items")
            mine = [row for row in rows if str(row.get("agent_id") or "") == agent_id]
            if delegation.resume_from:
                prior = [
                    row for row in rows if str(row.get("agent_id") or "") == delegation.resume_from
                ]
                mine = [*prior, *mine]
            return await system_and_messages(
                SessionView(
                    session_id=parent.session_id,
                    items=mine,
                    capabilities=tuple(item.pack.id for item in catalogue.ready()),
                    session=dict(session),
                    response_style=parent.policy.response_style,
                    **view_limits(parent.policy),
                ),
                notice=combined,
            )

        async def execute(plan: dict[str, Any]) -> dict[str, Any]:
            return await self.capabilities.execute(plan, child)

        async def append(kind: str, role: str, content: object) -> None:
            await self.store.append(
                parent.account_id,
                parent.session_id,
                NewItem(kind, role, content, agent_id=agent_id),
            )

        outcome = await run_turn(
            Turn(
                provider=provider,
                assemble=assemble,
                execute=execute,
                plan_schema=self.capabilities.plan_schema(catalogue, parent.session_id, child),
                append=append,
                model=str(session["model"]),
                budget=Budget(max_iterations=delegation.max_iterations),
                max_output_tokens=parent.policy.max_output_tokens,
                temperature=parent.policy.temperature,
                thinking=parent.policy.thinking,
                result_token_cap=parent.policy.agent_result_token_cap,
                max_thinking_tokens=parent.policy.max_thinking_tokens,
            )
        )
        summary, tokens, notice = capped_summary(outcome.text or outcome.detail)
        data, schema_notice = declared_return(summary, delegation.return_schema)
        if schema_notice:
            notice = f"{notice}; {schema_notice}".strip("; ")
        status = "ok"
        if outcome.termination not in {Termination.success, Termination.refused}:
            status = "failed"
        return {
            "status": status,
            "agent_id": agent_id,
            "role": delegation.role,
            "summary": summary,
            "data": data,
            "tokens": tokens,
            "notice": notice,
            "termination": outcome.termination.value,
            "permission_mode": child.permission_mode,
        }


__all__ = ["ChildRuntime"]
