"""Liveness never fails; readiness tells the truth about keyring."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import FakeKeyring

from lucy_api.api.app import create_app
from lucy_api.core.config import Settings

if TYPE_CHECKING:
    from httpx import AsyncClient as Client


@pytest.mark.asyncio
async def test_healthy_does_no_io_and_never_fails(client: Client) -> None:
    response = await client.get("/healthy")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["uptime_seconds"] >= 0
    assert "version" in body


@pytest.mark.asyncio
async def test_ready_is_ok_when_keyring_answers(client: Client) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["keyring"]["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_is_503_when_keyring_keys_cannot_be_fetched(
    settings: Settings, keyring: FakeKeyring
) -> None:
    # Readiness is the route that is allowed to fail, and it must say which dependency did.
    keyring.error = httpx.ConnectError("down")
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        response = await http.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["keyring"]["detail"]["reachable"] is False


@pytest.mark.asyncio
async def test_health_routes_name_no_account(client: Client) -> None:
    # A health document that moves when one person acts is an oracle.
    for path in ("/healthy", "/ready"):
        body = (await client.get(path)).text
        assert "acct_" not in body
