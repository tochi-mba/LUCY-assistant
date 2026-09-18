"""The live-state source for everything still in flight.

The registry already knows how to snapshot itself. This is the thin `Source` wrapper the
context engine expects, so a turn can gather work alongside memory and workspace without
the assembler importing the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.context.types import WorkSnapshot
    from lucy_api.work.registry import Registry


@dataclass(frozen=True, slots=True)
class WorkInFlight:
    """What is running, or just finished, for one session.

    `announce` is off for prompt previews so inspecting the context does not eat the
    "finished since last turn" flag a real turn has not seen yet.
    """

    registry: Registry
    announce: bool = False

    async def fetch(self, session_id: str) -> Sequence[WorkSnapshot]:
        return self.registry.snapshot(session_id, announce=self.announce)


__all__ = ["WorkInFlight"]
