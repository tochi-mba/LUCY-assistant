"""A profile's sandbox, given back.

The bug, named: every profile got a sandbox and nothing ever gave one back, so an account
that made short-lived profiles -- one per eval run, then one per eval conversation -- filled
the sandbox's cap of twenty, and from then on every new session in a new profile answered
503 "could not be provisioned".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import Environment, FakeEnvironmentsClient
from lucy_api.packs.http import DownstreamError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container


@pytest.fixture
async def hub(
    settings: Settings, keyring: FakeKeyring
) -> AsyncIterator[tuple[AsyncClient, Container, FakeEnvironmentsClient]]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        sandbox = FakeEnvironmentsClient()
        container.environment_override = sandbox
        yield http, container, sandbox


async def a_session_in(http: AsyncClient, profile: str, key: str) -> str:
    created = await http.post(
        "/v1/sessions",
        json={"profile": profile},
        headers={**bearer(), "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_finished_profile_gives_its_sandbox_back(
    hub: tuple[AsyncClient, Container, FakeEnvironmentsClient],
) -> None:
    http, container, sandbox = hub
    session = await a_session_in(http, "eval-1", "release-1")
    assert len(sandbox.workspaces) == 1
    archived = await http.patch(
        f"/v1/sessions/{session}", json={"archived": True}, headers=bearer()
    )
    assert archived.status_code == 200

    released = await http.delete("/v1/workspaces/eval-1", headers=bearer())

    assert released.status_code == 200
    assert released.json() == {"profile": "eval-1", "released": True}
    assert sandbox.workspaces == {}
    row = await container.store.get(ACCOUNT, session)
    assert row["workspace_environment_id"] is None, "nothing points at the destroyed sandbox"


async def test_a_profile_with_no_sandbox_says_there_was_nothing_to_release(
    hub: tuple[AsyncClient, Container, FakeEnvironmentsClient],
) -> None:
    http, _container, sandbox = hub
    sandbox.seed(Environment(environment_id="env-other", name="someone-else", profile="eval-2"))

    released = await http.delete("/v1/workspaces/eval-2", headers=bearer())

    assert released.json() == {"profile": "eval-2", "released": False}
    assert "env-other" in sandbox.workspaces, "only the profile's own sandbox, by its name"


async def test_a_profile_still_in_use_keeps_its_sandbox(
    hub: tuple[AsyncClient, Container, FakeEnvironmentsClient],
) -> None:
    http, _container, sandbox = hub
    await a_session_in(http, "work", "release-2")

    refused = await http.delete("/v1/workspaces/work", headers=bearer())

    assert refused.status_code == 409
    assert "not archived" in refused.json()["detail"]
    assert len(sandbox.workspaces) == 1


async def test_a_sandbox_that_cannot_be_reached_is_a_503(
    hub: tuple[AsyncClient, Container, FakeEnvironmentsClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, _container, sandbox = hub

    async def down(*, profile: str = "") -> tuple[Environment, ...]:
        del profile
        raise DownstreamError("down", audience="environments", status=502)

    monkeypatch.setattr(sandbox, "environments", down)

    answered = await http.delete("/v1/workspaces/eval-3", headers=bearer())

    assert answered.status_code == 503
    assert "could not be reached to release it" in answered.json()["detail"]
