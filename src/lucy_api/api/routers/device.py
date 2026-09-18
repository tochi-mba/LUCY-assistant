"""Passwordless command-line sign-in using short-lived device codes."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response, status

from lucy_api.api.dependencies import ActingAsDep, ContainerDep
from lucy_api.api.schemas.device import (
    DeviceCodeResource,
    DeviceDecisionRequest,
    DeviceTokenRequest,
    DeviceTokenResource,
)
from lucy_api.api.schemas.problem import Problem
from lucy_api.auth.device import DEVICE_SECONDS

router = APIRouter(tags=["authentication"])
_PROBLEM = {"model": Problem}


@router.post(
    "/v1/auth/device",
    operation_id="create_device_authorization",
    summary="Start command-line sign-in",
    response_model=DeviceCodeResource,
    status_code=status.HTTP_201_CREATED,
    description="Creates a short-lived code. It does not require or accept a password.",
)
async def create_device_authorization(
    request: Request, container: ContainerDep
) -> DeviceCodeResource:
    code = await container.device_flow.create()
    origin = str(request.base_url).rstrip("/")
    verification_uri = f"{origin}/device"
    query = urlencode({"user_code": code.user_code})
    return DeviceCodeResource(
        device_code=code.device_code,
        user_code=code.user_code,
        verification_uri=verification_uri,
        verification_uri_complete=f"{verification_uri}?{query}",
        expires_in=DEVICE_SECONDS,
        interval=code.interval,
    )


@router.post(
    "/v1/auth/device/token",
    operation_id="redeem_device_authorization",
    summary="Poll for command-line sign-in",
    response_model=DeviceTokenResource,
    responses={status.HTTP_400_BAD_REQUEST: {"description": "RFC 8628 device-flow error."}},
)
async def redeem_device_authorization(
    body: DeviceTokenRequest, container: ContainerDep
) -> DeviceTokenResource:
    token = await container.device_flow.poll(body.device_code)
    return DeviceTokenResource(access_token=token.access_token)


@router.post(
    "/v1/auth/device/authorize",
    operation_id="decide_device_authorization",
    summary="Approve or deny a command-line sign-in",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        status.HTTP_400_BAD_REQUEST: _PROBLEM,
        status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    },
    description=(
        "Requires an existing authenticated Lucy client. Approval transfers that client's "
        "subject-bound Lucy token to the device that holds the unguessable device code."
    ),
)
async def decide_device_authorization(
    body: DeviceDecisionRequest, acting: ActingAsDep, container: ContainerDep
) -> Response:
    await container.device_flow.decide(
        body.user_code,
        account_id=acting.account_id,
        access_token=acting.token,
        approve=body.approve,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
