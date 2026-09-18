"""Account-authenticated, read-only setup discovery for clients."""

from __future__ import annotations

from fastapi import APIRouter, Request

from lucy_api.api.dependencies import ActingAsDep, ContainerDep
from lucy_api.clients.errors import DownstreamError
from lucy_api.core.container import PackRequest
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
        "Deployment readiness is separate from the person's connection state. Connection "
        "state is read from the vault for this account; unknown means that inspect failed "
        "this request. This read never starts authorization, opens a browser, or obtains "
        "a provider credential."
    ),
)
async def get_setup(
    request: Request, acting: ActingAsDep, container: ContainerDep
) -> SetupResponse:
    """Scope the response to the verified caller; readiness probes remain credential-free."""
    discovery: SetupDiscovery = request.app.state.onboarding
    result = await discovery.discover(acting.account_id)
    return discovery.with_connections(result, await _vault_status(acting, container))


async def _vault_status(acting: ActingAsDep, container: ContainerDep) -> dict[str, str] | None:
    """Statuses keyed by vault service name, or none when the vault cannot be read."""
    bound = PackRequest(
        caller=acting.caller,
        user_token=acting.token,
        profile="personal",
        session_id="",
    )
    try:
        rows = await container.connection_client(bound).connections("personal")
    except DownstreamError:
        return None
    return {item.service: item.status for item in rows}
