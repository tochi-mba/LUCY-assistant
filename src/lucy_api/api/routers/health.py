"""Liveness and readiness.

``/healthy`` does no I/O and never fails. ``/ready`` reports each dependency and answers
503 when one is unusable. Point a container healthcheck at the first and a load balancer at
the second.

Readiness checks three things: keyring's signing keys, the session database, and at least
one configured model provider. A hub can expose setup without a model, but it cannot accept
conversation traffic until it has somewhere to send the assembled request.

Neither route is authenticated, and neither reports a name, an account or a count that
moves when one person acts. A counter that moves when one person acts is an oracle.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from lucy_api import __version__
from lucy_api.api.dependencies import ContainerDep
from lucy_api.api.schemas.health import CheckResult, LivenessResponse, ReadyResponse

router = APIRouter(tags=["health"])

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


@router.get(
    "/healthy",
    operation_id="check_liveness",
    summary="Whether the process is running",
    response_model=LivenessResponse,
    description=(
        "Does no I/O and never fails. Answering means the process is alive; it says nothing "
        "about whether it can serve a request, which is what `/ready` is for."
    ),
)
async def check_liveness(container: ContainerDep) -> LivenessResponse:
    """Liveness only: the process is running. No I/O, and it never fails."""
    return LivenessResponse(
        status="alive",
        version=__version__,
        environment=container.settings.environment,
        uptime_seconds=round(container.uptime_seconds, 3),
    )


@router.get(
    "/ready",
    operation_id="check_readiness",
    summary="Whether the process can serve a request",
    response_model=ReadyResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
    description=(
        "Checks keyring's signing keys, the database and model-provider configuration. "
        "Answers 503 when either is unusable, because a request that needs one of them "
        "would fail anyway and failing at the load balancer is cheaper than failing at the "
        "route."
    ),
)
async def check_readiness(container: ContainerDep, response: Response) -> ReadyResponse:
    """Report whether tokens can be verified and conversations can be read."""
    usable, reason = await container.jwks.healthy()
    stored, database_reason = await container.store.healthy()
    checks = {
        "keyring": CheckResult(
            status=STATUS_OK if usable else STATUS_DEGRADED,
            detail={"reachable": reason is None, "reason": reason},
        ),
        "database": CheckResult(
            status=STATUS_OK if stored else STATUS_DEGRADED,
            detail={"reachable": database_reason is None, "reason": database_reason},
        ),
        "model": CheckResult(
            status=STATUS_OK if container.turns.configured else STATUS_DEGRADED,
            detail={"configured": container.turns.configured},
        ),
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
