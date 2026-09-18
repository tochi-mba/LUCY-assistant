"""Long-run push: register a destination, never a payload.

A webhook learns that a turn ended, parked, or needs a credential. The transcript stays
behind `GET /v1/turns/{id}`. The signing secret is returned once, on create.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response, status

from lucy_api.api.dependencies import ContainerDep, CurrentCallerDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.api.schemas.webhooks import CreateWebhook, WebhookPage, WebhookResource

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_403_FORBIDDEN: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_409_CONFLICT: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
HookId = Annotated[str, Path(min_length=1, max_length=64)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="create_webhook",
    summary="Register a signal destination",
    response_model=WebhookResource,
    responses=_ADDRESSED,
    description=(
        "Lucy will POST `{session_id, turn_id, status}` signed with `X-Lucy-Signature: "
        "sha256=…`. The body is never a transcript. The signing secret is in this response "
        "only; listing the same hook later does not repeat it. Registering the same URL "
        "again returns the existing hook without a new secret."
    ),
)
async def create_webhook(
    caller: CurrentCallerDep, container: ContainerDep, body: CreateWebhook
) -> WebhookResource:
    return WebhookResource.model_validate(
        await container.webhooks.register(caller.account_id, body.url)
    )


@router.get(
    "",
    operation_id="list_webhooks",
    summary="Destinations this account has registered",
    response_model=WebhookPage,
    responses=_ADDRESSED,
    description="Secrets are never listed. Delete and recreate to rotate one.",
)
async def list_webhooks(caller: CurrentCallerDep, container: ContainerDep) -> WebhookPage:
    rows = await container.webhooks.for_account(caller.account_id)
    return WebhookPage(data=[WebhookResource.model_validate(row) for row in rows])


@router.delete(
    "/{hook_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_webhook",
    summary="Stop signalling a destination",
    responses=_ADDRESSED,
    description="A stranger's id is a 404, identical to one that never existed.",
)
async def delete_webhook(
    caller: CurrentCallerDep, container: ContainerDep, hook_id: HookId
) -> Response:
    await container.webhooks.delete(caller.account_id, hook_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
