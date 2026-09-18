"""The installed permission catalogue and a person's effective grants, as HTTP.

The gate consults this ledger before a write runs. Creating a grant is how a client records
an answer without waiting for the next tool call to ask again. Deleting one returns that
permission to "not yet asked".
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field

from lucy_api.api.dependencies import ContainerDep, CurrentCallerDep, StoreDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.core.errors import LucyError
from lucy_api.permissions.store import delete_grant, list_grants, put_grant

router = APIRouter(prefix="/v1/permissions", tags=["permissions"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
_WRITES: dict[int | str, dict[str, Any]] = {
    **_ADDRESSED,
    status.HTTP_400_BAD_REQUEST: _PROBLEM,
}
DECISIONS = frozenset({"allow", "deny"})
UNKNOWN_DECISION = "Unknown decision `{decision}`; this collection has `allow`, `deny`"
BAD_REQUEST = "bad-request"
PermissionId = Annotated[str, Path(min_length=1, max_length=128)]
ProfileQuery = Annotated[str, Query(min_length=1, max_length=128)]


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission: str = Field(min_length=1, max_length=128)
    decision: str = Field(min_length=1, max_length=32)
    profile: str = Field(default="personal", min_length=1, max_length=128)
    instruction: str = Field(default="", max_length=500)


@router.get(
    "",
    operation_id="list_permissions",
    summary="List installed permissions and their current grants",
    responses=_ADDRESSED,
    description=(
        "Every permission declared by an installed capability, whether decided or not. "
        "A profile grant overrides an account-wide grant."
    ),
)
async def list_permission_grants(
    caller: CurrentCallerDep,
    store: StoreDep,
    container: ContainerDep,
    profile: ProfileQuery = "personal",
) -> dict[str, Any]:
    grants = await list_grants(store, caller.account_id)
    effective: dict[str, dict[str, object]] = {}
    for grant in grants:
        scope = str(grant["profile"])
        if scope in {"*", profile}:
            effective[str(grant["permission"])] = grant

    data: list[dict[str, object]] = []
    seen: set[str] = set()
    for pack in container.capabilities.packs:
        for permission in pack.permissions():
            if permission.id in seen:
                continue
            seen.add(permission.id)
            data.append(
                {
                    "id": permission.id,
                    "permission": permission.id,
                    "title": permission.title,
                    "description": permission.description,
                    "risk": permission.risk,
                    "covers": list(permission.covers),
                    "grant": effective.get(permission.id),
                }
            )
    data.sort(key=lambda item: str(item["id"]))
    return {"profile": profile, "data": data}


@router.put(
    "",
    operation_id="put_permission",
    summary="Remember an allow or a deny",
    responses=_WRITES,
    description="Upserts one grant. An assistant cannot widen this through the settings pack.",
)
async def remember_grant(
    caller: CurrentCallerDep, store: StoreDep, body: GrantBody
) -> dict[str, Any]:
    if body.decision not in DECISIONS:
        message = UNKNOWN_DECISION.format(decision=body.decision)
        raise LucyError(BAD_REQUEST, message, 400)
    return await put_grant(
        store,
        caller.account_id,
        permission=body.permission,
        profile=body.profile,
        decision=body.decision,
        instruction=body.instruction,
    )


@router.delete(
    "/{permission_id}",
    operation_id="delete_permission",
    summary="Forget one grant",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_ADDRESSED,
    description="The next write that needs this permission will ask again.",
)
async def forget_grant(
    caller: CurrentCallerDep,
    store: StoreDep,
    permission_id: PermissionId,
    profile: ProfileQuery = "personal",
) -> None:
    await delete_grant(store, caller.account_id, permission_id, profile=profile)
