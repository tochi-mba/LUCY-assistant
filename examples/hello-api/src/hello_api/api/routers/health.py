"""Liveness and readiness."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from hello_api import __version__
from hello_api.api.dependencies import ContainerDep
from hello_api.api.schemas import CheckResult, LivenessResponse, ReadyResponse

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


@router.get("/healthy", response_model=LivenessResponse)
async def get_health(container: ContainerDep) -> LivenessResponse:
    """Liveness only: no I/O, never fails."""
    return LivenessResponse(
        status="alive",
        version=__version__,
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
    )


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def check_readiness(container: ContainerDep, response: Response) -> ReadyResponse:
    """Report whether keyring's signing keys can be fetched."""
    usable, reason = await container.jwks.healthy()
    checks = {
        "keyring": CheckResult(
            status=STATUS_OK if usable else STATUS_DEGRADED,
            detail={"reachable": reason is None, "reason": reason},
        )
    }
    healthy = all(check.status == STATUS_OK for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status=STATUS_OK if healthy else STATUS_DEGRADED,
        version=__version__,
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
        checks=checks,
    )
