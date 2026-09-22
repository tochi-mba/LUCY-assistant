"""Which models a person can use right now, and what the rest would need.

Two reads. The first is the whole catalogue in three sections; the second is one provider,
for a client that is about to offer a button. Both are authenticated: the report says which
keys this deployment holds and whether they work, which is nobody's business but the
people the hub serves.

Neither route touches a provider unless asked. `check=true` proves the configured keys with
one listing call each, remembered for a minute; the default answers from the catalogue and
whatever was proven recently, so a settings page can render without forty network calls.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, status

from lucy_api.api.dependencies import ContainerDep, CurrentCallerDep
from lucy_api.api.schemas.models import ModelsResource, StandingResource
from lucy_api.api.schemas.problem import Problem
from lucy_api.core.errors import LucyError
from lucy_api.model.catalogue import spec_for

router = APIRouter(prefix="/v1", tags=["models"])

_PROBLEM: dict[str, Any] = {"model": Problem}
UNKNOWN_PROVIDER = "unknown-provider"
"""The problem code for a provider id the catalogue has no row for."""


@router.get(
    "/models",
    operation_id="list_models",
    summary="Every model provider, sorted by whether you can use it",
    response_model=ModelsResource,
    response_model_exclude_none=True,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM},
    description=(
        "Three sections. `ready` providers are configured and answered a listing call; "
        "`available` ones are configured but not proven; `unavailable` ones say exactly what "
        "is missing and the `lucy` command that supplies it. Pass `check=true` to prove the "
        "configured keys now rather than reuse the last check."
    ),
)
async def list_models(
    caller: CurrentCallerDep,
    container: ContainerDep,
    check: bool = Query(
        default=False, description="Prove configured keys with one listing call each."
    ),
) -> ModelsResource:
    """The catalogue, sorted."""
    del caller  # authentication is the point; the report is the same for everybody
    report = await container.readiness.report(prove=check)
    return ModelsResource.model_validate(report.as_dict())


@router.get(
    "/models/{provider}",
    operation_id="get_model_provider",
    summary="One provider's standing",
    response_model=StandingResource,
    response_model_exclude_none=True,
    responses={status.HTTP_401_UNAUTHORIZED: _PROBLEM, status.HTTP_404_NOT_FOUND: _PROBLEM},
    description=(
        "The same row `GET /v1/models` would show for this provider, on its own. A provider "
        "the hub has no row for is a 404 that names the models route, never a guess at a "
        "near miss."
    ),
)
async def get_model_provider(
    provider: str,
    caller: CurrentCallerDep,
    container: ContainerDep,
    check: bool = Query(default=False, description="Prove this provider's key now."),
) -> StandingResource:
    """One row of the catalogue."""
    del caller
    if spec_for(provider) is None:
        detail = f"no model provider called {provider!r}; GET /v1/models lists them"
        raise LucyError(UNKNOWN_PROVIDER, detail, status.HTTP_404_NOT_FOUND)
    report = await container.readiness.report(prove=check)
    return StandingResource.model_validate(report.standing(provider))


__all__ = ["router"]
