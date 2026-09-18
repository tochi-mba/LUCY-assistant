"""The journal as a live-state source: one gather, one failure domain.

The assembler must not import the agent store. This wrapper is the Source protocol, so a
journal outage costs the tasks group and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.agents.store import AgentStore
    from lucy_api.context.types import TaskSnapshot


@dataclass(frozen=True, slots=True)
class JournalLive:
    """This account's journal, fetched for one session."""

    store: AgentStore
    account: str

    async def fetch(self, session_id: str) -> Sequence[TaskSnapshot]:
        return await self.store.tasks(self.account, session_id)


__all__ = ["JournalLive"]
