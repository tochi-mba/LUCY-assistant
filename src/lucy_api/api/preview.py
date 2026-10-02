"""A session as its next turn would see it, read without running one.

`GET /context`, `GET /context/window` and the compact route's before-and-after all need the
same thing: the transcript, the active compactions, the capabilities that are ready and the
person's limits, put together exactly the way the turn loop puts them together. Built in one
place so the preview and the figure a person is shown cannot drift from what a turn sends.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lucy_api.core.container import PackRequest
from lucy_api.turn.prompt import SessionView, conversation_order, schema_tokens, view_limits

if TYPE_CHECKING:
    from lucy_api.api.dependencies import ActingAs
    from lucy_api.core.container import Container


async def session_view(acting: ActingAs, container: Container, session_id: str) -> SessionView:
    """The view a turn would assemble from, for the verified caller's session."""
    store = container.store
    session = await store.get(acting.account_id, session_id)
    items = await store.records(acting.account_id, session_id, "items")
    compactions = await store.records(acting.account_id, session_id, "compactions")
    turns = await store.records(acting.account_id, session_id, "turns")
    prepared = await container.prepare_turn(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=str(session["profile"]),
            session_id=session_id,
            permission_mode=str(session.get("permission_mode", "ask")),
            incognito=bool(session.get("incognito", 0)),
        ),
        session,
    )
    catalogue = await container.capabilities.probe(prepared.pack_context)
    ready = tuple(item.pack.id for item in catalogue.ready())
    visible = {str(turn["id"]) for turn in turns}
    parent_items = [row for row in items if not row.get("agent_id")]
    policy = prepared.pack_context.policy
    return SessionView(
        session_id=session_id,
        items=conversation_order(parent_items, turns, visible),
        capabilities=ready,
        session=session,
        compactions=compactions,
        turn_number=sum(1 for turn in turns if turn["status"] == "completed") + 1,
        live=prepared.live,
        response_style=policy.response_style,
        schema_tokens=schema_tokens(
            container.capabilities.plan_schema(catalogue, session_id, prepared.pack_context)
        ),
        **view_limits(policy),
    )


__all__ = ["session_view"]
