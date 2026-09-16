"""Authenticated whoami route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from keyring_client.testing import FakeKeyring, mint

from hello_api.auth.verifier import KEYS_UNAVAILABLE
from hello_api.core.config import Settings

if TYPE_CHECKING:
    from httpx import AsyncClient

ACCOUNT = "acct_example"
AUDIENCE = "hello"


def _bearer(account_id: str = ACCOUNT, audience: str = AUDIENCE) -> dict[str, str]:
    token = mint(account_id=account_id, audience=audience)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_whoami_returns_account(client: AsyncClient) -> None:
    response = await client.get("/v1/whoami", headers=_bearer())
    assert response.status_code == 200
    assert response.json() == {"account_id": ACCOUNT, "audience": AUDIENCE}


@pytest.mark.asyncio
async def test_whoami_requires_token(client: AsyncClient) -> None:
    response = await client.get("/v1/whoami")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_whoami_refuses_wrong_audience(client: AsyncClient) -> None:
    response = await client.get("/v1/whoami", headers=_bearer(audience="other-service"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_whoami_503_when_keyring_keys_cannot_be_fetched(
    settings: Settings, keyring: FakeKeyring
) -> None:
    # A valid token, but keyring is down before its keys were ever cached: the service
    # cannot tell whether the token is good, and says so with 503 rather than 401.
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from hello_api.api.app import create_app

    keyring.error = httpx.ConnectError("down")
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        response = await http.get("/v1/whoami", headers=_bearer())
    assert response.status_code == 503
    assert response.json()["detail"] == KEYS_UNAVAILABLE
