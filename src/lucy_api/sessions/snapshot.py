"""The small, durable state sent before an SSE stream's deltas."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lucy_api.sessions.sql_store import SessionStore


class SessionSnapshotter:
    """Adapt the session store to the stream's snapshot seam.

    Authorization is deliberately not repeated here.  The event route verifies ownership
    before it calls ``EventEmitter.subscribe``; the emitter has no request or account and
    must not grow a second authorization mechanism.
    """

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        """Return only session state a reconnecting client needs to redraw itself."""
        row = await self._store.stream_snapshot(session_id)
        return {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "model": row["model"],
            "input_policy": row["input_policy"],
            "permission_mode": row["permission_mode"],
            "updated_at": row["updated_at"],
            "latest_turn": row["latest_turn"],
        }
