"""Who the hub thinks you are.

One authenticated route, and the cheapest possible one, because it is the first call a
client makes and the one that tells it whether its token is for *this* service. It returns
the subject from the verified token and nothing the caller supplied.
"""

from __future__ import annotations

from fastapi import APIRouter

from lucy_api.api.dependencies import CurrentCallerDep
from lucy_api.api.schemas.me import MeResponse

router = APIRouter(prefix="/v1", tags=["identity"])


@router.get("/me", response_model=MeResponse)
async def get_me(caller: CurrentCallerDep) -> MeResponse:
    """Return the account the presented token is for."""
    return MeResponse(account_id=caller.account_id, audience=caller.audience)
