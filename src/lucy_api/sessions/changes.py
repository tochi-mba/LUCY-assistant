"""Changing how a session behaves, including while it is in the middle of a turn.

A title can change whenever. The three fields that change what a running turn *may do* --
the double-text policy, the permission mode, and which capabilities are turned off -- take
effect on the running turn at its next round, and that is a thing a person should have to
mean. So a change to one of them while a turn is live is answered with a warning rather
than applied, and the caller answers the warning by saying which they want:

- `apply: "now"` applies it to the running turn. The next model round sees the new mode
  and the new capability list, and a step the model already planned against a capability
  that is now off is refused with a sentence rather than run.
- `apply: "after_turn"` holds it. It lands the moment the turn ends, with its own event
  first and an `updated` event marked `held` second, so a client can show both.

Sending neither, while a turn is live, is the warning: a 409 whose detail names the turn,
the fields, and the two answers. Sending either while the session is idle is an ordinary
update; there is nothing to warn about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import conflict
from lucy_api.sessions.models import BEHAVIOUR

if TYPE_CHECKING:
    from lucy_api.sessions.models import UpdateSession
    from lucy_api.sessions.sql_store import SessionStore

APPLY_NOW = "now"
APPLY_AFTER = "after_turn"


def warning(turn_id: str, fields: list[str]) -> str:
    """The sentence that stands in for the change, with the two ways to answer it."""
    named = ", ".join(fields)
    return (
        f"A turn ({turn_id}) is running and {named} would change it mid-turn. Send the same "
        f"change with apply='{APPLY_NOW}' to apply it to the running turn, or "
        f"apply='{APPLY_AFTER}' to hold it until that turn ends."
    )


async def change_session(
    store: SessionStore, account: str, session_id: str, request: UpdateSession
) -> dict[str, Any]:
    """Apply, hold, or warn. Returns the session as it stands afterwards."""
    changes = request.model_dump(exclude_unset=True)
    apply = changes.pop("apply", None)
    behaviour = sorted(name for name in changes if name in BEHAVIOUR and changes[name] is not None)
    if not behaviour:
        return await store.update(account, session_id, changes)
    live = await store.live_turn(account, session_id)
    if live is None:
        return await store.update(account, session_id, changes)
    if apply == APPLY_NOW:
        return await store.update(account, session_id, {**changes, "during_turn": live})
    if apply == APPLY_AFTER:
        return await store.hold_changes(account, session_id, changes)
    raise conflict(warning(live, behaviour))


__all__ = ["APPLY_AFTER", "APPLY_NOW", "change_session", "warning"]
