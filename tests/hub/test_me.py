"""The one authenticated route: it answers from the token, never from the request."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, FakeKeyring, mint

from lucy_api.api.app import create_app
from lucy_api.core.config import Settings

if TYPE_CHECKING:
    from httpx import AsyncClient as Client

ACCOUNT = "acct_example"
AUDIENCE = "lucy-api"


def _bearer(account_id: str = ACCOUNT, audience: str = AUDIENCE) -> dict[str, str]:
    token = mint(account_id=account_id, audience=audience, issuer=ISSUER)
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_me_returns_the_subject_of_the_token(client: Client) -> None:
    response = await client.get("/v1/me", headers=_bearer())
    assert response.status_code == 200
    assert response.json() == {"account_id": ACCOUNT, "audience": AUDIENCE}


@pytest.mark.asyncio
async def test_me_without_a_token_is_401(client: Client) -> None:
    response = await client.get("/v1/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_with_another_services_token_is_401(client: Client) -> None:
    response = await client.get("/v1/me", headers=_bearer(audience="persona"))
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_me_is_503_not_401_when_keyring_keys_are_unreachable(
    settings: Settings, keyring: FakeKeyring
) -> None:
    # The token may be perfectly good. Saying 401 here would send somebody to log in again
    # to fix a problem that is not theirs.
    keyring.error = httpx.ConnectError("down")
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        response = await http.get("/v1/me", headers=_bearer())
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
