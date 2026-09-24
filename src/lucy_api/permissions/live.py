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

An approval is described by what it would do, not by the permission it needs. The row's
`description` is the gate's sentence for the permission -- "Remember and change notes about
you needs approval before it can run" -- which is the same for every notes write there will
ever be. Given only that, Lucy told a person "`notes.setFact` is still waiting on your
approval, and I can't see what it would record", about an ask whose arguments said
`title: Occupation; body: Backend engineer, mostly Python.` The arguments are what tell
one ask from the next, so they are what the line carries.

Elicitations stay empty. `input.elicitation_response` is a declared input event and nothing
in the hub writes the request it would answer, so there is no pending elicitation to find.
Reporting none is true; inventing a source for it here would not be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lucy_api.context.types import PendingSnapshot
from lucy_api.permissions.approvals import PENDING

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.connections.tickets import ConnectionTickets
    from lucy_api.sessions.sql_store import SessionStore

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
                "SELECT operation, description, input_json FROM approvals "
                "WHERE session_id=? AND status=? ORDER BY requested_at LIMIT ?",
                (session_id, PENDING, MOST),
            ).fetchall()
            return tuple(
                _line(row["operation"], _asked(row["input_json"]) or row["description"])
                for row in rows
            )

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


def _line(operation: object, detail: object) -> str:
    """What the model reads. The operation first, because that is what it would call again."""
    name = str(operation or "").strip()
    said = str(detail or "").strip()
    return f"{name} -- {said}" if name and said else name or said


def _asked(input_json: object) -> str:
    """The ask's own arguments, as `key: value` pairs, or nothing to go on.

    Only plain values: a nested object would be rendered as a repr the model has to parse,
    and the renderer cuts the line at its detail width anyway, so what is worth carrying is
    the short identifying fields a person would recognise -- a title, a path, a query.
    """
    try:
        payload: Any = json.loads(str(input_json or ""))
    except ValueError:
        return ""
    arguments = payload.get("arguments") if isinstance(payload, dict) else None
    if not isinstance(arguments, dict):
        return ""
    return "; ".join(
        f"{key}: {str(value).strip()}"
        for key, value in arguments.items()
        if isinstance(value, str | int | float) and str(value).strip()
    )


__all__ = ["MOST", "PendingLive"]
