"""Who the hub thinks you are.

One authenticated route, and the cheapest possible one, because it is the first call a
client makes and the one that tells it whether its token is for *this* service. It returns
the subject from the verified token and nothing the caller supplied.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, CurrentCallerDep
from lucy_api.api.schemas.me import MeResponse
from lucy_api.api.schemas.problem import Problem
from lucy_api.core.container import PackRequest

router = APIRouter(prefix="/v1", tags=["identity"])

_PROBLEM: dict[str, Any] = {"model": Problem}


@router.get(
    "/me",
    operation_id="whoami",
    summary="Who the hub believes you are",
    response_model=MeResponse,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
    description=(
        "Returns the account id and audience taken from the verified bearer token, and "
        "nothing else. It is the cheapest way to check a token without writing anything, "
        "and the one route whose response *is* the identity -- which makes it the place to "
        "notice if verification ever starts believing the wrong subject."
    ),
)
async def whoami(caller: CurrentCallerDep) -> MeResponse:
    """Return the account the presented token is for."""
    return MeResponse(account_id=caller.account_id, audience=caller.audience)


@router.delete(
    "/account",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="erase_account",
    summary="Wipe Lucy's copy of this account",
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
    description=(
        "Deletes every session, uploaded file, artifact, grant and pinned MCP server Lucy "
        "holds for the verified account. Memory-api is a different store: facts learned "
        "there survive until that service's own erasure path runs. This is not reversible."
    ),
)
async def erase_account(acting: ActingAsDep, container: ContainerDep) -> Response:
    """Drop Lucy's local copy of this person."""
    await container.erase_account(
        PackRequest(
            caller=acting.caller,
            user_token=acting.token,
            profile="",
            session_id="",
        )
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
