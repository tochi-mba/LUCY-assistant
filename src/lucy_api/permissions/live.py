"""The live-state source for everything waiting on the person.

`PendingSnapshot`, `_pending_group`, the `pending` quota and the three "waiting for ..."
lines were all written, and `Sources.pending` was never given anything, so the band has
never once appeared in a prompt.

What that costs is not abstract. Ask Lucy to remember two things, let it park for approval,
type something else instead of answering, and three turns later ask what it knows about you:
it lists what it has and ends "That is everything" -- with two facts it asked about sitting
unanswered in the same session and nothing in front of it saying so. A parked turn does not
resume on its own and nothing ever mentions it again.

Approvals come from the `approvals` table, over the `approvals_open` index that was created
for this query and had no reader. Connections come from the ticket registry, which is
process-local and per person rather than per session: a consent link the person has not
opened yet is exactly "waiting to connect", wherever it was asked for.

Elicitations stay empty. `input.elicitation_response` is a declared input event and nothing
in the hub writes the request it would answer, so there is no pending elicitation to find.
Reporting none is true; inventing a source for it here would not be.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.context.types import PendingSnapshot

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.connections.tickets import ConnectionTickets
    from lucy_api.sessions.sql_store import SessionStore

OPEN = "pending"
"""The `approvals.status` of one that has been asked and not answered."""

MOST = 8
"""How many of each kind to carry.

The band has a quota of its own and will trim further; this only stops a pathological
session from loading the whole table to have it thrown away one line later.
"""


@dataclass(frozen=True, slots=True)
class PendingLive:
    """What this session is waiting on the person for."""

    store: SessionStore
    tickets: ConnectionTickets | None = None
    account_id: str = ""
    profile: str = ""

    async def fetch(self, session_id: str) -> PendingSnapshot:
        return PendingSnapshot(
            approvals=await self._approvals(session_id),
            connections=self._connections(),
        )

    async def _approvals(self, session_id: str) -> tuple[str, ...]:
        def read(db: sqlite3.Connection) -> tuple[str, ...]:
            rows = db.execute(
                "SELECT operation, description FROM approvals "
                "WHERE session_id=? AND status=? ORDER BY requested_at LIMIT ?",
                (session_id, OPEN, MOST),
            ).fetchall()
            return tuple(_line(row["operation"], row["description"]) for row in rows)

        return await self.store.worker.call(read)

    def _connections(self) -> tuple[str, ...]:
        """Consent links this person has been handed and not yet opened.

        Not filtered by session on purpose: a ticket is per account and profile, and a
        connection the person started in another conversation is still a connection this
        turn should not assume has happened.
        """
        if self.tickets is None or not self.account_id:
            return ()
        return tuple(
            ticket.service
            for ticket in self.tickets.outstanding(self.account_id, self.profile)[:MOST]
        )


def _line(operation: object, description: object) -> str:
    """What the model reads. The operation first, because that is what it would call again."""
    name = str(operation or "").strip()
    detail = str(description or "").strip()
    return f"{name} -- {detail}" if name and detail else name or detail


__all__ = ["MOST", "OPEN", "PendingLive"]
