"""Helpers a restart stopped, told to the conversation that started them.

The roster is durable and the work registry is not. On start, the supervisor marks every
helper a previous process left running as interrupted; this is the half that makes sure the
conversation that started one finds out. Without it, a helper that was mid-task when the
hub restarted vanished: the notice its parent was promised never came, `agents.list`
showed only what was running, and nothing on the next turn said anything had happened.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lucy_api.agents.types import CONTINUABLE, RESTARTED
from lucy_api.work.types import Brief, Kind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.agents.store import Interrupted
    from lucy_api.work.registry import Registry

TOP_LEVEL = 1
"""The depth of a helper the conversation itself started.

Only those are announced. A deeper helper's parent was a helper, which stopped with it, and
continuing that parent is how its work resumes; telling the conversation about a
grandchild it never started would be news about a stranger.
"""


def announce_interrupted(work: Registry | None, stopped: Sequence[Interrupted]) -> tuple[str, ...]:
    """Record each conversation's own interrupted helpers as ended, once. Returns their ids."""
    if work is None:
        return ()
    announced: list[str] = []
    for helper in stopped:
        if helper.depth != TOP_LEVEL:
            continue
        work.record_lost(
            Brief(
                session_id=helper.session_id,
                kind=Kind.helper,
                role=helper.role,
                objective=helper.objective,
                depth=helper.depth,
                account_id=helper.account_id,
            ),
            work_id=helper.id,
            started_at=datetime.fromtimestamp(helper.started_at, UTC),
            ended_at=datetime.fromtimestamp(helper.last_seen, UTC),
            detail=f"{CONTINUABLE}: {RESTARTED}",
            payload={
                "status": "failed",
                "agent_id": helper.id,
                "role": helper.role,
                "summary": RESTARTED,
                "tokens": 0,
                "resumable": True,
            },
        )
        announced.append(helper.id)
    return tuple(announced)


__all__ = ["TOP_LEVEL", "announce_interrupted"]
