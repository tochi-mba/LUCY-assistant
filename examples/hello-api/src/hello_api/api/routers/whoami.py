"""One authenticated route: echo the verified caller."""

from __future__ import annotations

from fastapi import APIRouter

from hello_api.api.dependencies import CurrentCallerDep
from hello_api.api.schemas import WhoAmIResponse

router = APIRouter(prefix="/v1", tags=["whoami"])


@router.get("/whoami", response_model=WhoAmIResponse)
async def whoami(caller: CurrentCallerDep) -> WhoAmIResponse:
    """Return the account id and audience from the verified Bearer token."""
    return WhoAmIResponse(account_id=caller.account_id, audience=caller.audience)
