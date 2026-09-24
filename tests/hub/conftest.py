"""Fixtures for the hub's own tests.

Everything runs in-process against `keyring_client.testing.FakeKeyring`, which serves real
RSA-signed tokens from a mock transport. No network, no live keyring, and no reason for a
test to know a port number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, JWKS_URL, FakeKeyring, mint
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.core.config import LogFormat, Settings
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

AUDIENCE = "lucy-api"
ACCOUNT = "acct_example"


def build_settings(**overrides: Any) -> Settings:
    """Settings that point at the fake keyring and read no `.env` from the developer's disk."""
    defaults: dict[str, Any] = {
        "_env_file": None,
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "audience": AUDIENCE,
        # In memory unless a test asks otherwise: the container now opens a database when it
        # is built, and the default path would scatter a `var/` directory through whatever
        # directory the suite happened to run in.
        "database_path": ":memory:",
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
        await app.state.container.preferences.aclose()
        app.state.container.preferences = FakeSettingsClient()
        app.state.container.environment_override = FakeEnvironmentsClient()
        yield http


@pytest.fixture
async def workspace_client(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[AsyncClient]:
    """`client`, with the workspace capability on the same fake sandbox that provisions each
    session -- so a session has a workspace its tools can really read and write -- and a
    scripted model, because a conversation on a model this hub cannot run is refused when
    it is created."""
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.models = ModelRegistry({"scripted": lambda _model: ScriptedProvider()})
        sandbox = FakeEnvironmentsClient()
        container.environment_override = sandbox
        for pack in container.capabilities.packs:
            if pack.id == "workspace":
                pack._override = sandbox
        yield http


@pytest.fixture
async def sessions_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    """A real database on disk, for the domain tests that have no HTTP in them.

    Real SQLite rather than a fake, because half of what those tests claim is transactional
    behaviour and the other half is SQL; a fake would test the fake. Closing it matters: an
    open WAL pins `tmp_path` on Windows and the teardown fails on the directory instead.
    """
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    store = SessionStore(worker)
    await store.initialize()
    try:
        yield store
    finally:
        await worker.aclose()


def bearer(account_id: str = ACCOUNT, audience: str = AUDIENCE) -> dict[str, str]:
    """A signed token for the hub's own audience."""
    token = mint(account_id=account_id, audience=audience, issuer=ISSUER)
    return {"Authorization": f"Bearer {token}"}
