"""Passwordless command-line sign-in using short-lived device codes."""

from __future__ import annotations

import html
import re
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse

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

USER_CODE = re.compile(r"^[A-Z0-9]{4}(-[A-Z0-9]{4})+$")
"""What a code the hub issued looks like; anything else is not shown back."""

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approve a Lucy sign-in</title>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 3rem auto;
             padding: 0 1rem; line-height: 1.5">
<h1>Approve a Lucy sign-in</h1>
<p>A Lucy client is waiting to sign in{waiting}.</p>
<p>On a machine where Lucy is already signed in as you, run:</p>
<pre>lucy approve {code}</pre>
<p>Nothing is entered here, and this page never asks for a password.</p>
<p>No other client is signed in? Sign the waiting one in with a token from your keyring
instead: <code>lucy setup --token-stdin</code>.</p>
</body>
</html>
"""


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


@router.get("/device", include_in_schema=False, response_class=HTMLResponse)
async def device_page(user_code: str = "") -> HTMLResponse:
    """Where the code a waiting client was given points: how to approve it, and nothing more.

    The hub sent clients here and served nothing, so `lucy setup` opened a 404. Approval
    comes from a client that is already signed in, so this page only says how.
    """
    shown = user_code if USER_CODE.fullmatch(user_code) else ""
    page = PAGE.format(
        waiting=f" with code <code>{html.escape(shown)}</code>" if shown else "",
        code=html.escape(shown) or "CODE",
    )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


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
