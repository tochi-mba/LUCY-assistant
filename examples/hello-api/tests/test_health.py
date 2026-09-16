"""Health probes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from keyring_client.testing import FakeKeyring

from hello_api.core.config import Settings

if TYPE_CHECKING:
    from httpx import AsyncClient


@pytest.mark.asyncio
async def test_healthy_never_fails(client: AsyncClient) -> None:
    response = await client.get("/healthy")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert "version" in body
    assert body["uptime_seconds"] >= 0


@pytest.mark.asyncio
async def test_ready_when_keyring_answers(client: AsyncClient) -> None:
    response = await client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["keyring"]["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_degraded_when_keyring_down(settings: Settings, keyring: FakeKeyring) -> None:
    from asgi_lifespan import LifespanManager
    from httpx import ASGITransport, AsyncClient

    from hello_api.api.app import create_app

    keyring.error = RuntimeError("down")
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        response = await http.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
