"""Authenticated whoami route."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from keyring_client import KeyringUnreachableError as KeysUnavailableError
from keyring_client.testing import FakeKeyring, mint

from hello_api.auth.verifier import KEYS_UNAVAILABLE, KeyringUnreachableError, TokenVerifier
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
async def test_verifier_maps_unreachable_keys() -> None:
    from keyring_client import ExactAudience

    inner = AsyncMock()
    inner.verify = AsyncMock(side_effect=KeysUnavailableError("down"))
    verifier = TokenVerifier.__new__(TokenVerifier)
    object.__setattr__(verifier, "_verifier", inner)
    object.__setattr__(verifier, "_audience", ExactAudience("hello"))
    with pytest.raises(KeyringUnreachableError, match=KEYS_UNAVAILABLE):
        await verifier.verify("not-used")


@pytest.mark.asyncio
async def test_whoami_503_when_keys_unavailable(settings: Settings, keyring: FakeKeyring) -> None:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from hello_api.api.app import create_app

    app = create_app(settings, transport=keyring.transport())

    async with LifespanManager(app):
        container = app.state.container

        async def boom(_token: str) -> None:
            raise KeyringUnreachableError(KEYS_UNAVAILABLE)

        object.__setattr__(container.verifier, "verify", boom)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/v1/whoami", headers=_bearer())
    assert response.status_code == 503
    assert response.json()["detail"] == KEYS_UNAVAILABLE
