"""Render the stable prompt without running a turn.

A session's assembled context lives under ``GET /v1/sessions/{id}/context`` because it
needs the transcript. This route is the prefix alone: identity, behaviour, tool idiom,
safety, and the names of what is connected. It is what you look at when a setting that
overrides a section has gone wrong, before you spend a generation to find out.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, StoreDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.core.container import PackRequest
from lucy_api.turn.prompt import preview_document

router = APIRouter(prefix="/v1", tags=["prompt"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_AUTHED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
}


@router.get(
    "/prompt/preview",
    operation_id="preview_prompt",
    summary="The stable prompt sections, without running a turn",
    responses=_AUTHED,
    description=(
        "Renders the identity, behaviour, tool-idiom and safety sections, plus the names "
        "of capabilities that are ready. Pass a session id to use that session's profile "
        "and any capabilities it has already bound."
    ),
)
async def preview_prompt(
    acting: ActingAsDep,
    container: ContainerDep,
    store: StoreDep,
    session_id: Annotated[str | None, Query(max_length=64)] = None,
    profile: Annotated[str, Query(min_length=1, max_length=128)] = "personal",
) -> dict[str, Any]:
    """The prefix a turn would send, priced and versioned."""
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
    ready = tuple(item.pack.id for item in catalogue.ready())
    return preview_document(capabilities=ready)
