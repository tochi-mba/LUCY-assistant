"""Helpers in flight, as a session sub-resource.

The model starts helpers through the agents pack. This read is for a client that wants the
same roster without executing a plan.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, status

from lucy_api.api.dependencies import ContainerDep, CurrentCallerDep, StoreDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.api.schemas.sessions import ItemResource, Page, SelectionDep, TurnResource
from lucy_api.core.errors import absent
from lucy_api.sessions.items import list_agent_items
from lucy_api.sessions.sql_store import page
from lucy_api.work.types import Kind

router = APIRouter(prefix="/v1/sessions", tags=["agents"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
}
SessionId = Annotated[str, Path(min_length=1, max_length=64)]
AgentId = Annotated[str, Path(min_length=1, max_length=64)]


@router.get(
    "/{session_id}/agents",
    operation_id="list_session_agents",
    summary="Helpers currently running on this conversation",
    responses=_ADDRESSED,
    description="In-flight helpers only. Finished ones are work notices, not this list.",
)
async def list_session_agents(
    caller: CurrentCallerDep,
    store: StoreDep,
    container: ContainerDep,
    session_id: SessionId,
) -> dict[str, Any]:
    await store.get(caller.account_id, session_id)
    running = [
        {
            "id": record.id,
            "role": record.role,
            "objective": record.objective,
            "depth": record.depth,
            "state": record.state.value,
        }
        for record in container.work.running(session_id)
        if record.kind is Kind.helper
    ]
    return {"data": running}


def _public_agent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role": row["role"],
        "objective": row["objective"],
        "status": row["status"],
        "depth": row["depth"],
        "created_at": row["created_at"],
        "finished_at": row.get("finished_at"),
        "interrupted_reason": row.get("interrupted_reason"),
    }


async def _owned_agent(
    container: ContainerDep, account: str, session_id: str, agent_id: str
) -> dict[str, Any]:
    await container.store.get(account, session_id)
    row = await container.agents.get(account, agent_id)
    if str(row.get("session_id") or "") != session_id:
        raise absent()
    return row


@router.get(
    "/{session_id}/subagents",
    operation_id="list_session_subagents",
    summary="Helpers this conversation started, running or finished",
    responses=_ADDRESSED,
    description=(
        "The durable roster. In-flight process memory is `GET /v1/sessions/{id}/agents`. "
        "A helper from another conversation is a 404, never a 403."
    ),
)
async def list_session_subagents(
    caller: CurrentCallerDep, container: ContainerDep, session_id: SessionId
) -> dict[str, Any]:
    rows = await container.agents.for_session(caller.account_id, session_id)
    return {"data": [_public_agent(row) for row in rows]}


@router.get(
    "/{session_id}/subagents/{agent_id}",
    operation_id="get_session_subagent",
    summary="One helper belonging to this conversation",
    responses=_ADDRESSED,
    description="A stranger's helper id is the same 404 as an unknown one.",
)
async def get_session_subagent(
    caller: CurrentCallerDep,
    container: ContainerDep,
    session_id: SessionId,
    agent_id: AgentId,
) -> dict[str, Any]:
    return _public_agent(await _owned_agent(container, caller.account_id, session_id, agent_id))


@router.get(
    "/{session_id}/subagents/{agent_id}/items",
    operation_id="list_subagent_items",
    summary="The helper's own transcript",
    response_model=Page[ItemResource],
    responses=_ADDRESSED,
    description="Parent items are not in this collection; helper items are not in the parent's.",
)
async def list_subagent_items(
    caller: CurrentCallerDep,
    store: StoreDep,
    container: ContainerDep,
    session_id: SessionId,
    agent_id: AgentId,
    selection: SelectionDep,
) -> Page[ItemResource]:
    await _owned_agent(container, caller.account_id, session_id, agent_id)
    raw = await list_agent_items(store, caller.account_id, session_id, agent_id, selection)
    return Page[ItemResource].model_validate(raw)


@router.get(
    "/{session_id}/subagents/{agent_id}/turns",
    operation_id="list_subagent_turns",
    summary="Turns belonging to a helper",
    response_model=Page[TurnResource],
    responses=_ADDRESSED,
    description=(
        "Helpers share the parent's turn row and do not have their own. This page is always "
        "empty, so a client that polls the same shape as the parent conversation gets an "
        "honest answer rather than a 404 that looks like the helper vanished."
    ),
)
async def list_subagent_turns(
    caller: CurrentCallerDep,
    container: ContainerDep,
    session_id: SessionId,
    agent_id: AgentId,
    selection: SelectionDep,
) -> Page[TurnResource]:
    await _owned_agent(container, caller.account_id, session_id, agent_id)
    raw = page([], selection.limit, selection.after, selection.before, selection.order)
    return Page[TurnResource].model_validate(raw)
