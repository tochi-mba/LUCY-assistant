"""The in-process owner of queued turns.

The HTTP request records intent and returns.  This supervisor owns the work after that
point, which is why closing an SSE connection cannot cancel a turn and why a restart can
claim work that was queued before it began.  It is intentionally a small scheduler: SQLite
is the queue, the session row is the lock, and the loop remains in :mod:`lucy_api.turn.loop`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from lucy_api.context.build import Live
from lucy_api.context.sources import Sources
from lucy_api.core.errors import LucyError
from lucy_api.core.logging import allow_message_content
from lucy_api.model.registry import UnknownModelError
from lucy_api.permissions.approvals import Ask, open_approval
from lucy_api.permissions.gate import PermissionGate
from lucy_api.permissions.store import grants_for
from lucy_api.sessions.compact import compact_session
from lucy_api.sessions.scope import scope_from_row
from lucy_api.sessions.sql_store import NewItem
from lucy_api.stream.emitter import NewEvent
from lucy_api.stream.events import TURN_SLOW
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.project import StreamProjector
from lucy_api.turn.prompt import (
    SessionView,
    conversation_order,
    projected_rows,
    system_and_messages,
    view_limits,
)
from lucy_api.turn.stop import Budget, Termination
from lucy_api.work.live import WorkInFlight

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from lucy_api.agents.store import AgentStore
    from lucy_api.model.registry import ModelRegistry
    from lucy_api.model.types import Message
    from lucy_api.packs.context import PackContext
    from lucy_api.packs.service import Capabilities
    from lucy_api.sessions.sql_store import SessionStore
    from lucy_api.stream.emitter import EventEmitter


@dataclass(frozen=True, slots=True)
class ClaimedTurn:
    """The small durable record a worker needs after it has claimed a turn."""

    id: str
    session_id: str
    account_id: str
    model: str
    thinking: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ClaimedTurn:
        return cls(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            account_id=str(row["account_id"]),
            model=str(row["model"]),
            thinking=str(row["thinking_config"]),
        )


@dataclass(frozen=True, slots=True)
class PreparedTurn:
    """Request-scoped authority and live inputs held only until this turn is claimed.

    Nothing here is durable. A recovered turn deliberately falls back to a context with no
    broker instead of persisting or replaying the credential that authorized the request.
    """

    pack_context: PackContext
    live: Live
    budget: Budget = field(default_factory=Budget)
    max_subagent_turns: int = 8


class TurnSupervisor:
    """Drain durable queued turns without a broker or a second writer process."""

    def __init__(  # noqa: PLR0913,PLR0917 - on_status is the webhook fan-out
        self,
        store: SessionStore,
        models: ModelRegistry,
        events: EventEmitter,
        capabilities: Capabilities | None = None,
        agents: AgentStore | None = None,
        on_status: Callable[[str, str, str, str], Awaitable[None]] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        from lucy_api.packs.service import (  # noqa: PLC0415 - packs imports turn
            Capabilities as Installed,
        )

        self._store = store
        self._models = models
        self._events = events
        self._capabilities = capabilities if capabilities is not None else Installed()
        self._agents = agents
        self._on_status = on_status
        self._sleeper = sleeper or asyncio.sleep
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._prepared: dict[str, PreparedTurn] = {}

    def authorize(self, turn_id: str, prepared: PreparedTurn) -> None:
        """Make in-memory request authority available to one queued turn."""
        if not self._closed and self.configured:
            self._prepared[turn_id] = prepared

    def discard(self, turn_id: str) -> None:
        """Release request authority when queued work is cancelled before claim."""
        self._prepared.pop(turn_id, None)

    async def start(self) -> None:
        """Fail turns a previous process left running, then drain what is still queued."""
        await self._store.interrupt_abandoned_turns()
        if self._agents is not None:
            await self._agents.interrupt_running()
        self.wake()

    @property
    def configured(self) -> bool:
        """Whether any provider credential exists to turn queued work into a response."""
        return bool(self._models.providers)

    def wake(self) -> None:
        """Ensure one drainer is running; repeated calls coalesce into that task."""
        if (
            self._closed
            or not self.configured
            or (self._task is not None and not self._task.done())
        ):
            return
        self._task = asyncio.create_task(self._drain(), name="lucy-turn-supervisor")

    async def aclose(self) -> None:
        """Stop scheduling new work; already recorded turns remain resumable."""
        self._closed = True
        self._prepared.clear()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def join(self) -> None:
        """Wait for currently scheduled work. Useful to deterministic in-process tests."""
        if self._task is not None:
            await self._task

    async def _drain(self) -> None:
        while not self._closed:
            row = await self._store.claim_next_turn()
            if row is None:
                return
            claimed = ClaimedTurn.from_row(row)
            try:
                await self._run(claimed)
            finally:
                await self._events.publish_persisted(claimed.session_id)

    async def _run(self, claimed: ClaimedTurn) -> None:  # noqa: PLR0915 - one turn is one function
        prepared = self._prepared.pop(claimed.id, None)
        try:
            provider = self._models.resolve(claimed.model)
        except Exception as exc:
            await self._finish_failure(
                claimed, f"model configuration failed ({type(exc).__name__})"
            )
            return

        session = await self._store.get(claimed.account_id, claimed.session_id)
        # One scope, derived from the stored row and the account the token proved. Every
        # capability, every client call and every child this turn starts reads it rather
        # than re-deriving the profile from somewhere else.
        scope = replace(scope_from_row(session, account_id=claimed.account_id), turn_id=claimed.id)
        pack_ctx = (
            prepared.pack_context if prepared is not None else self._capabilities.context_for(scope)
        )
        pack_ctx.grants = await grants_for(
            self._store,
            claimed.account_id,
            str(session["profile"]),
            session_id=claimed.session_id,
            turn_id=claimed.id,
        )
        live = _announced(prepared.live if prepared is not None else None, self._capabilities.work)
        catalogue = await self._capabilities.probe(pack_ctx)
        ready = tuple(item.pack.id for item in catalogue.ready())
        policy = pack_ctx.policy
        advertised = _advertised(policy.enabled, ready, policy.disabled)
        oriented = False
        compacted = False

        fallback_provider = None
        fallback_model = ""
        if policy.fallback_model and policy.fallback_model != claimed.model:
            with contextlib.suppress(UnknownModelError):
                fallback_provider = self._models.resolve(policy.fallback_model)
                fallback_model = policy.fallback_model

        async def cancelled() -> bool:
            row = await self._store.turn(claimed.account_id, claimed.id)
            return bool(row.get("cancel_requested"))

        async def assemble(notice: str) -> tuple[str, tuple[Message, ...]]:
            nonlocal oriented, compacted
            rows = await self._store.records(claimed.account_id, claimed.session_id, "items")
            turns = await self._store.records(claimed.account_id, claimed.session_id, "turns")
            compact = await self._store.records(
                claimed.account_id, claimed.session_id, "compactions"
            )
            visible_turns = {
                str(turn["id"])
                for turn in turns
                if turn["status"] in {"completed", "failed", "cancelled"}
                or turn["id"] == claimed.id
            }
            ordered = conversation_order(_items_for(rows, pack_ctx.agent_id), turns, visible_turns)
            turn_number = sum(1 for turn in turns if turn["status"] == "completed") + 1
            _arm_workspace(live, resume=turn_number > 1 and not oriented)
            oriented = True
            view = SessionView(
                session_id=claimed.session_id,
                items=ordered,
                capabilities=ready,
                advertised=advertised,
                session=session,
                compactions=compact,
                turn_number=turn_number,
                live=live,
                response_style=policy.response_style,
                **view_limits(policy),
            )
            _rows, reclaimed = projected_rows(view)
            if reclaimed.should_compact and not compacted:
                compacted = True
                with contextlib.suppress(LucyError):
                    await compact_session(
                        self._store,
                        claimed.account_id,
                        claimed.session_id,
                        keep_recent=policy.history_turns_kept,
                    )
                    view = replace(
                        view,
                        compactions=await self._store.records(
                            claimed.account_id, claimed.session_id, "compactions"
                        ),
                    )
            notices = [notice]
            if not policy.vision_enabled and _looks_like_images(ordered):
                notices.append("Vision is off. Image attachments were not sent to the model.")
            return await system_and_messages(view, notice="\n".join(filter(None, notices)))

        async def execute(plan: dict[str, Any]) -> dict[str, Any]:
            executed = await self._capabilities.execute(plan, pack_ctx)
            await self._store.record_steps(
                claimed.account_id, claimed.session_id, claimed.id, plan, executed
            )
            if pack_ctx.permission_mode in {"auto", "accept_edits"}:
                await _audit_bypasses(self._store, claimed, plan, pack_ctx)
            return executed

        async def append(kind: str, role: str, content: object) -> None:
            await self._store.append(
                claimed.account_id,
                claimed.session_id,
                NewItem(kind, role, content, turn=claimed.id),
            )

        slow: asyncio.Task[None] | None = None
        if policy.notify_on_long_turn:
            slow = asyncio.create_task(
                self._notify_slow(claimed, policy.long_turn_seconds),
                name=f"lucy-turn-slow-{claimed.id}",
            )
        try:
            with allow_message_content(policy.log_message_content):
                result = await run_turn(
                    Turn(
                        provider=provider,
                        assemble=assemble,
                        execute=execute,
                        plan_schema=self._capabilities.plan_schema(
                            catalogue, claimed.session_id, pack_ctx
                        ),
                        append=append,
                        model=claimed.model,
                        budget=prepared.budget if prepared is not None else None,
                        max_output_tokens=pack_ctx.policy.max_output_tokens,
                        temperature=pack_ctx.policy.temperature,
                        thinking=pack_ctx.policy.thinking,
                        result_token_cap=pack_ctx.policy.max_tool_result_tokens,
                        cancelled=cancelled,
                        on_chunk=StreamProjector(
                            self._events,
                            claimed.session_id,
                            claimed.id,
                            stream_thinking=policy.stream_thinking,
                        ),
                        fallback_provider=fallback_provider,
                        fallback_model=fallback_model,
                        max_thinking_tokens=policy.max_thinking_tokens,
                    )
                )
                await self._finish_result(claimed, result, pack_ctx, session)
        finally:
            if slow is not None:
                slow.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await slow

    async def _notify_slow(self, claimed: ClaimedTurn, seconds: int) -> None:
        await self._sleeper(seconds)
        await self._events.emit(
            claimed.session_id,
            NewEvent(TURN_SLOW, {"seconds": seconds}, turn_id=claimed.id),
        )

    async def _finish_result(
        self,
        claimed: ClaimedTurn,
        result: Any,
        pack_ctx: PackContext,
        session: dict[str, Any],
    ) -> None:
        if result.termination is Termination.input_required:
            asks = result.asks or (
                {
                    "permission": result.permission,
                    "operation": result.operation,
                    "message": result.detail,
                    "arguments": result.arguments,
                    "description": result.description,
                },
            )
            for ask in asks:
                arguments = ask.get("arguments")
                await open_approval(
                    self._store,
                    account=claimed.account_id,
                    session_id=claimed.session_id,
                    turn_id=claimed.id,
                    ask=Ask(
                        permission=str(ask.get("permission") or ""),
                        operation=str(ask.get("operation") or ""),
                        description=str(
                            ask.get("description") or ask.get("message") or result.detail
                        ),
                        arguments=arguments if isinstance(arguments, dict) else {},
                        policy=pack_ctx.permission_mode,
                        termination=result.termination.value,
                        stop_reason=result.stop_reason.value,
                    ),
                )
            await self._signal(claimed, "input_required")
            return
        status = _status_for(result.termination)
        await self._store.finish_turn(
            claimed.account_id,
            claimed.id,
            status,
            result.termination.value,
            result.stop_reason.value,
        )
        if status == "completed":
            await _maybe_title(self._store, claimed, pack_ctx, session)
        await self._signal(claimed, status)

    async def _finish_failure(self, claimed: ClaimedTurn, detail: str) -> None:
        await self._store.append(
            claimed.account_id,
            claimed.session_id,
            NewItem(
                "error",
                "assistant",
                {"code": "model_unavailable", "detail": detail},
                turn=claimed.id,
            ),
        )
        await self._store.finish_turn(
            claimed.account_id,
            claimed.id,
            "failed",
            Termination.failed.value,
        )
        await self._signal(claimed, "failed")

    async def _signal(self, claimed: ClaimedTurn, status: str) -> None:
        if self._on_status is None:
            return
        await self._on_status(claimed.account_id, claimed.session_id, claimed.id, status)


def _advertised(
    enabled: tuple[str, ...], ready: tuple[str, ...], disabled: tuple[str, ...]
) -> tuple[str, ...]:
    """Disconnected capabilities this profile asked to hear about. Empty means stay quiet."""
    if not enabled:
        return ()
    usable = frozenset(ready)
    off = frozenset(disabled)
    return tuple(name for name in enabled if name not in usable and name not in off)


def _looks_like_images(value: object) -> bool:
    """Whether this turn's items look like they carry an image the model cannot see."""
    if isinstance(value, str):
        lowered = value.lower()
        return lowered.startswith("image/") or lowered in {"image", "input_image", "image_url"}
    if isinstance(value, dict):
        mime = str(value.get("mime") or value.get("media_type") or value.get("mime_type") or "")
        kind = str(value.get("type") or value.get("kind") or "")
        if mime.lower().startswith("image/") or kind.lower() in {
            "image",
            "input_image",
            "image_url",
        }:
            return True
        return any(_looks_like_images(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_looks_like_images(item) for item in value)
    return False


UNTITLED = frozenset({"", "New conversation"})


async def _maybe_title(
    store: SessionStore,
    claimed: ClaimedTurn,
    pack_ctx: PackContext,
    session: dict[str, Any],
) -> None:
    """Name an untitled parent conversation from the first user message, once."""
    if pack_ctx.agent_id or not pack_ctx.policy.auto_title:
        return
    if str(session.get("title") or "") not in UNTITLED:
        return
    rows = await store.records(claimed.account_id, claimed.session_id, "items")
    text = _first_user_text(_items_for(rows, ""))
    if not text:
        return
    await store.update(claimed.account_id, claimed.session_id, {"title": text[:80]})


def _first_user_text(rows: list[dict[str, Any]]) -> str:
    for row in rows:
        if str(row.get("type") or "message") != "message":
            continue
        if row.get("role") != "user":
            continue
        content = row.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, dict):
            text = content.get("text") or content.get("content")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _arm_workspace(live: Live | None, *, resume: bool) -> None:
    """Spend the resume ritual on the first assemble of a returning turn, and nowhere else."""
    sources = None if live is None else live.sources
    workspace = None if sources is None else sources.workspace
    arm = getattr(workspace, "arm", None)
    if callable(arm):
        arm(resume)


def _announced(live: Live | None, work: Any) -> Live | None:
    """A real turn may consume the 'finished since last turn' flag; a preview may not."""
    if work is None:
        return live
    current = live if live is not None else Live()
    sources = current.sources if current.sources is not None else Sources()
    return replace(
        current,
        sources=replace(sources, in_flight=WorkInFlight(work, announce=True)),
    )


async def _audit_bypasses(
    store: SessionStore, claimed: ClaimedTurn, plan: dict[str, Any], pack_ctx: PackContext
) -> None:
    """Every auto-mode write is a decision somebody can find later."""
    verdict = PermissionGate().inspect(
        plan,
        mode=pack_ctx.permission_mode,
        grants=pack_ctx.grants,
        catalogue=pack_ctx.catalogue,
        memory_write_policy=pack_ctx.policy.memory_write_policy,
        confirm_outward=pack_ctx.policy.confirm_outward_actions,
    )
    for permission in verdict.auto_bypassed:
        await store.record_audit(
            claimed.account_id,
            "permission.auto",
            session=claimed.session_id,
            turn=claimed.id,
            detail={"permission": permission, "mode": pack_ctx.permission_mode},
        )


def _items_for(rows: list[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
    """A helper has its own item log; the parent must not see it, and vice versa."""
    if agent_id:
        return [row for row in rows if str(row.get("agent_id") or "") == agent_id]
    return [row for row in rows if not row.get("agent_id")]


def _conversation_order(
    items: list[dict[str, Any]], turns: list[dict[str, Any]], visible_turns: set[str]
) -> list[dict[str, Any]]:
    """Kept under the old name so existing tests keep importing from this module."""
    return conversation_order(items, turns, visible_turns)


def _status_for(termination: Termination) -> str:
    if termination is Termination.success or termination is Termination.refused:
        return "completed"
    if termination is Termination.cancelled:
        return "cancelled"
    if termination is Termination.input_required:
        return "input_required"
    if termination is Termination.auth_required:
        return "auth_required"
    return "failed"


__all__ = ["ClaimedTurn", "PreparedTurn", "TurnSupervisor"]
