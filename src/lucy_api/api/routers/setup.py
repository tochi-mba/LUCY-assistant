"""Account-authenticated, read-only setup discovery for clients."""

from __future__ import annotations

from fastapi import APIRouter, Request

from lucy_api.api.dependencies import CurrentCallerDep
from lucy_api.onboarding.models import SetupResponse
from lucy_api.onboarding.service import SetupDiscovery

router = APIRouter(prefix="/v1", tags=["setup"])


@router.get(
    "/setup",
    operation_id="get_setup",
    response_model=SetupResponse,
    summary="Discover setup steps and deployment readiness",
    description=(
        "Lists required identity and optional capabilities with their setup instructions. "
        "Deployment readiness is separate from the person's connection state: unknown "
        "means Lucy cannot yet inspect it. This read never starts authorization, opens a "
        "browser, or obtains a provider credential."
    ),
)
async def get_setup(request: Request, caller: CurrentCallerDep) -> SetupResponse:
    """Scope the response to the verified caller; readiness probes remain credential-free."""
    discovery: SetupDiscovery = request.app.state.onboarding
    return await discovery.discover(caller.account_id)
