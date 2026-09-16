"""Shared fixtures."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, JWKS_URL, FakeKeyring, mint

from hello_api.api.app import create_app
from hello_api.core.config import LogFormat, Settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

AUDIENCE = "hello"
ACCOUNT = "acct_example"


def build_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "audience": AUDIENCE,
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def keyring() -> FakeKeyring:
    return FakeKeyring()


@pytest.fixture
async def client(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


def bearer(account_id: str = ACCOUNT, audience: str = AUDIENCE) -> dict[str, str]:
    token = mint(account_id=account_id, audience=audience, issuer=ISSUER)
    return {"Authorization": f"Bearer {token}"}
