"""Project a model stream onto the session event log.

The OpenAI adapter's own note is the rule: when a plan was asked for, text deltas *are*
the plan's JSON. Forwarding those to a person would show them the wire format of a tool
call. Reasoning is display, but only when ``lucy.stream_thinking`` is on -- working-out is
where secrets and half-plans show up, so the catalogue default is off. Spoken text waits
for the `done` chunk, and is published only when that chunk carries no plan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lucy_api.model.wire import CHUNK_DONE, CHUNK_REASONING, CHUNK_TEXT
from lucy_api.stream.emitter import NewEvent
from lucy_api.stream.events import (
    CONTENT_REASONING_DELTA,
    CONTENT_REASONING_END,
    CONTENT_REASONING_START,
    CONTENT_TEXT_DELTA,
    CONTENT_TEXT_END,
    CONTENT_TEXT_START,
)

if TYPE_CHECKING:
    from lucy_api.model.types import Chunk
    from lucy_api.stream.emitter import EventEmitter


class StreamProjector:
    """One turn's worth of stream state. Constructed per `_run`, discarded after it."""

    def __init__(
        self,
        events: EventEmitter,
        session_id: str,
        turn_id: str,
        *,
        stream_thinking: bool = False,
    ) -> None:
        self._events = events
        self._session_id = session_id
        self._turn_id = turn_id
        self._stream_thinking = stream_thinking
        self._spoken: list[str] = []
        self._reasoning_open = False

    async def __call__(self, chunk: Chunk) -> None:
        if chunk.kind == CHUNK_REASONING and chunk.text:
            if not self._stream_thinking:
                return
            if not self._reasoning_open:
                await self._emit(CONTENT_REASONING_START, {})
                self._reasoning_open = True
            await self._emit(CONTENT_REASONING_DELTA, {"delta": chunk.text})
            return
        if chunk.kind == CHUNK_TEXT and chunk.text:
            self._spoken.append(chunk.text)
            return
        if chunk.kind != CHUNK_DONE:
            return
        if self._reasoning_open:
            await self._emit(CONTENT_REASONING_END, {})
            self._reasoning_open = False
        reply = chunk.reply
        spoken = "".join(self._spoken)
        self._spoken.clear()
        if reply is None or reply.plan is not None or not spoken:
            return
        await self._emit(CONTENT_TEXT_START, {})
        await self._emit(CONTENT_TEXT_DELTA, {"delta": spoken})
        await self._emit(CONTENT_TEXT_END, {})

    async def _emit(self, kind: str, data: dict[str, str]) -> None:
        await self._events.emit(
            self._session_id,
            NewEvent(type=kind, data=data, turn_id=self._turn_id),
        )
