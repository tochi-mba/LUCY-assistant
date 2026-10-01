"""Siblings end subscriptions here: one signed signal per subscription, never a payload.

A signal is not a person and carries no bearer token. It proves itself with the HMAC of its
raw body under the secret Lucy handed the sibling when the subscription was opened
(`X-Lucy-Signature: sha256=...`, the scheme Lucy's own webhooks use). An unknown
subscription and a signature that does not verify are the same 404, so guessing ids teaches
nothing; one that already ended is 409, so a sibling that retries knows it was heard.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Path, Request, Response, status

from lucy_api.api.dependencies import ContainerDep
from lucy_api.api.schemas.problem import Problem
from lucy_api.net.signing import HEADER

router = APIRouter(prefix="/v1/signals", tags=["signals"])

_PROBLEM: dict[str, Any] = {"model": Problem}
SubscriptionId = Annotated[str, Path(min_length=1, max_length=64)]


@router.post(
    "/{subscription_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="send_signal",
    summary="End a subscription from the sibling that was watching for it",
    responses={
        status.HTTP_404_NOT_FOUND: _PROBLEM,
        status.HTTP_409_CONFLICT: _PROBLEM,
        status.HTTP_413_CONTENT_TOO_LARGE: _PROBLEM,
        status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
    },
    description=(
        "Body: `{state: fired|failed|expired, summary, facts?, excerpt?}`, at most 8 KiB, "
        "signed as `X-Lucy-Signature: sha256=<hex HMAC-SHA256 of the raw body>` with the "
        "subscription's secret. Says that the condition held, never the result: the woken "
        "turn reads that through the capability. Unknown id and bad signature are the same "
        "404; an ended subscription is 409."
    ),
)
async def send_signal(
    subscription_id: SubscriptionId,
    request: Request,
    container: ContainerDep,
    signature: Annotated[str | None, Header(alias=HEADER)] = None,
) -> Response:
    body = await request.body()
    await container.subscriptions.signal(subscription_id, signature, body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
