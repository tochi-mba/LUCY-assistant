"""A profile's sandbox, given back when its conversations are done with it."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, status

from lucy_api.api.dependencies import ActingAsDep, ContainerDep
from lucy_api.api.routers.sessions import WORKSPACE_UNAVAILABLE
from lucy_api.api.schemas.problem import Problem
from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError as ClientDownstreamError
from lucy_api.core.container import PackRequest
from lucy_api.core.errors import LucyError
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportDownstreamError
from lucy_api.workspace.release import release_profile_workspace

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])

_PROBLEM: dict[str, Any] = {"model": Problem}

ProfilePath = Annotated[str, Path(min_length=1, max_length=128)]


@router.delete(
    "/{profile}",
    operation_id="release_profile_workspace",
    summary="Destroy a profile's sandbox once no conversation uses it",
    responses={
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
        status.HTTP_409_CONFLICT: _PROBLEM,
        status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM,
    },
    description=(
        "Every profile gets one sandbox, and the sandbox caps how many an account holds. "
        "This destroys the profile's, with everything in it, so the slot is free again; a "
        "later conversation in the profile gets a new, empty one. Refused with 409 while the "
        "profile has conversations that are not archived. `released` is false when the "
        "profile had no sandbox. Not reversible."
    ),
)
async def release_workspace(
    profile: ProfilePath, acting: ActingAsDep, container: ContainerDep
) -> dict[str, Any]:
    """Free the sandbox slot a finished profile was holding."""
    request = PackRequest(
        caller=acting.caller,
        user_token=acting.token,
        profile=profile,
        session_id="",
        permission_mode="ask",
        incognito=False,
    )
    try:
        return await release_profile_workspace(container, request)
    except (
        ClientDownstreamError,
        ExchangeError,
        NoBrokerError,
        TransportDownstreamError,
    ) as exc:
        message = "The sandbox could not be reached to release it; retry when it is available."
        raise LucyError(WORKSPACE_UNAVAILABLE, message, 503) from exc
