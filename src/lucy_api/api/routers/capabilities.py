"""What the model can do, and what it can actually see this turn.

Two reads, on purpose. ``GET /v1/capabilities`` is the catalogue joined with connection
state: every pack this deployment installed, whether it is usable for this person, and a
sentence saying why not if it is not. ``GET /v1/tools`` is the registry the model would
be bound with right now, after deferred loading. A client that only has the first cannot
answer "why did it not call music.play"; a client that only has the second cannot offer
a connect button.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, StoreDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.core.container import PackRequest

router = APIRouter(prefix="/v1", tags=["capabilities"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_AUTHED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
}


@router.get(
    "/capabilities",
    operation_id="list_capabilities",
    summary="Every capability, its state, and one line each",
    responses=_AUTHED,
    description=(
        "The catalogue a person sees. An unconnected capability is still listed here so "
        "the UI can offer to connect it; the model's tool list hides it until it is ready."
    ),
)
async def list_capabilities(
    acting: ActingAsDep,
    container: ContainerDep,
    profile: Annotated[str, Query(min_length=1, max_length=128)] = "personal",
) -> dict[str, Any]:
    """Probe every installed pack for this person and return the listings."""
    pack_ctx = container.pack_context(
        PackRequest(caller=acting.caller, user_token=acting.token, profile=profile, session_id="")
    )
    catalogue = await container.capabilities.probe(pack_ctx)
    return {"data": container.capabilities.listings(catalogue)}


@router.get(
    "/tools",
    operation_id="list_model_tools",
    summary="The operations the model can actually call this turn",
    responses=_AUTHED,
    description=(
        "Names, one-line descriptions, and which capabilities were deferred. Pass a "
        "session id to include anything that session has already bound with "
        "`capabilities.use`."
    ),
)
async def list_model_tools(
    acting: ActingAsDep,
    container: ContainerDep,
    store: StoreDep,
    session_id: Annotated[str | None, Query(max_length=64)] = None,
    profile: Annotated[str, Query(min_length=1, max_length=128)] = "personal",
) -> dict[str, Any]:
    """The bound registry, not the whole catalogue."""
    resolved_profile = profile
    session = session_id or ""
    if session_id is not None:
        row = await store.get(acting.account_id, session_id)
        resolved_profile = str(row["profile"])
    pack_ctx = container.pack_context(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile=resolved_profile,
            session_id=session,
        )
    )
    catalogue = await container.capabilities.probe(pack_ctx)
    return container.capabilities.tools(catalogue, session)
