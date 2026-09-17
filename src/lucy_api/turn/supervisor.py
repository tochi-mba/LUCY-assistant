"""The in-process owner of queued turns.

The HTTP request records intent and returns.  This supervisor owns the work after that
point, which is why closing an SSE connection cannot cancel a turn and why a restart can
claim work that was queued before it began.  It is intentionally a small scheduler: SQLite
is the queue, the session row is the lock, and the loop remains in :mod:`lucy_api.turn.loop`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from lucy_api.sessions.scope import scope_from_row
from lucy_api.sessions.sql_store import NewItem
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.prompt import SessionView, system_and_messages
from lucy_api.turn.stop import Termination

if TYPE_CHECKING:
    from lucy_api.model.registry import ModelRegistry
    from lucy_api.model.types import Message
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


class TurnSupervisor:
    """Drain durable queued turns without a broker or a second writer process."""

    def __init__(
        self,
        store: SessionStore,
        models: ModelRegistry,
        events: EventEmitter,
        capabilities: Capabilities | None = None,
    ) -> None:
        from lucy_api.packs.service import (  # noqa: PLC0415 - packs imports turn
            Capabilities as Installed,
        )

        self._store = store
        self._models = models
        self._events = events
        self._capabilities = capabilities if capabilities is not None else Installed()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Recover turns left queued by a previous process."""
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

    async def _run(self, claimed: ClaimedTurn) -> None:
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
        pack_ctx = self._capabilities.context_for(scope)
        catalogue = await self._capabilities.probe(pack_ctx)
        ready = tuple(item.pack.id for item in catalogue.ready())

        async def assemble(notice: str) -> tuple[str, tuple[Message, ...]]:
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
            ordered = _conversation_order(rows, turns, visible_turns)
            turn_number = sum(1 for turn in turns if turn["status"] == "completed") + 1
            return await system_and_messages(
                SessionView(
                    session_id=claimed.session_id,
                    items=ordered,
                    capabilities=ready,
                    session=session,
                    compactions=compact,
                    turn_number=turn_number,
                ),
                notice=notice,
            )

        async def execute(plan: dict[str, Any]) -> dict[str, Any]:
            return await self._capabilities.execute(plan, pack_ctx)

        async def append(kind: str, role: str, content: object) -> None:
            await self._store.append(
                claimed.account_id,
                claimed.session_id,
                NewItem(kind, role, content, turn=claimed.id),
            )

        result = await run_turn(
            Turn(
                provider=provider,
                assemble=assemble,
                execute=execute,
                plan_schema=self._capabilities.plan_schema(catalogue, claimed.session_id),
                append=append,
                model=claimed.model,
            )
        )
        status = _status_for(result.termination)
        await self._store.finish_turn(
            claimed.account_id,
            claimed.id,
            status,
            result.termination.value,
            result.stop_reason.value,
        )

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


def _conversation_order(
    items: list[dict[str, Any]], turns: list[dict[str, Any]], visible_turns: set[str]
) -> list[dict[str, Any]]:
    """Put completed turns before later input that was queued while they ran.

    The append-only log records a second person's message the moment it arrives, which can
    precede the first turn's eventual assistant reply. The model must instead see each
    completed turn as a coherent exchange, then the input it is answering; otherwise it
    reads an answer after the question that followed it and mistakes normal enqueueing for
    contradictory conversation order.
    """
    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["turn_id"], []).append(item)
    ordered = list(grouped.pop(None, ()))
    for turn in turns:
        turn_id = str(turn["id"])
        if turn_id in visible_turns:
            ordered.extend(grouped.pop(turn_id, ()))
    return ordered


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


__all__ = ["ClaimedTurn", "TurnSupervisor"]
