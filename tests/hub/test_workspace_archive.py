"""A workspace the sandbox archived is reset before use, never picked again as it stands.

Environments-api archives an environment once it has sat idle past its time to live (a day
by default), wipes its workspace, and keeps listing it under the same name
(`Environments-api/app/environments/service.py`, `_archive`). Every file and shell call on
it is then a 409 `environment_archived` until `POST /v1/environments/{id}/reset`. The hub
picks the profile's environment by name, so without a reset every new session found the
archived one, was refused, and answered 503 "retry" to a retry that could never succeed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from test_sessions_api import Hub, create

from lucy_api.api.app import create_app
from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.clients.environments import ARCHIVED, SERVICE, FakeEnvironmentsClient
from lucy_api.clients.errors import ConflictError
from lucy_api.core.container import PackRequest
from lucy_api.sessions.scope import PROGRESS_FILE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings


@pytest.fixture
async def hub(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[Hub]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.environment_override = FakeEnvironmentsClient()
        yield Hub(http=http, store=container.store, container=container)


def _fake(hub: Hub) -> FakeEnvironmentsClient:
    fake = hub.container.environment_override
    assert isinstance(fake, FakeEnvironmentsClient)
    return fake


def _pack_request(session_id: str) -> PackRequest:
    return PackRequest(VerifiedCaller(ACCOUNT, "lucy-api"), "user-jwt", "personal", session_id)


async def test_a_new_session_on_an_archived_environment_resets_it_rather_than_failing(
    hub: Hub,
) -> None:
    """The bug, named: the archived environment was chosen by name forever, so every new
    session on the profile was a 503 "workspace could not be provisioned; retry"."""
    first = await create(hub, key="before-the-archive")
    fake = _fake(hub)
    env_id = str(first["workspace_environment_id"])
    fake.archive(env_id)

    second = await create(hub, key="after-the-archive")

    assert second["workspace_environment_id"] == env_id
    assert fake.resets == [env_id]
    assert fake.workspaces[env_id].state != ARCHIVED
    assert (env_id, f"{second['workspace_rel']}/{PROGRESS_FILE}") in fake.contents
    assert len(fake.workspaces) == 1


async def test_an_active_environment_is_never_reset_for_a_new_session(hub: Hub) -> None:
    """A reset wipes every session's folder, so it is reserved for the archived state."""
    first = await create(hub, key="one")
    await create(hub, key="two")

    assert _fake(hub).resets == []
    rel = first["workspace_rel"]
    assert (first["workspace_environment_id"], f"{rel}/{PROGRESS_FILE}") in _fake(hub).contents


async def test_resetting_a_session_on_an_archived_environment_brings_it_back(hub: Hub) -> None:
    """A session attached before the archive is not left on 409s: its own reset revives the
    environment and seeds its folder again, which the archive had already emptied."""
    session = await create(hub, key="attached-before")
    fake = _fake(hub)
    env_id = str(session["workspace_environment_id"])
    fake.archive(env_id)

    reset = await hub.http.post(f"/v1/sessions/{session['id']}/workspace/reset", headers=bearer())

    assert reset.status_code == 200, reset.text
    assert reset.json()["status"] == "attached"
    assert fake.resets == [env_id]
    assert (env_id, f"{session['workspace_rel']}/{PROGRESS_FILE}") in fake.contents


async def test_a_session_reset_leaves_alone_an_environment_another_session_revived(
    hub: Hub,
) -> None:
    """Resetting is whole-environment. If a new session revived it between this session's
    refusal and the revive, a second reset would wipe that new session's folder."""

    class Raced(FakeEnvironmentsClient):
        async def mkdir(self, environment_id: str, path: str) -> str:
            try:
                return await super().mkdir(environment_id, path)
            except ConflictError:
                await self.reset(environment_id)
                self.contents[(environment_id, "sessions/newcomer/progress.md")] = "theirs"
                raise

    session = await create(hub, key="raced")
    env_id = str(session["workspace_environment_id"])
    raced = Raced()
    raced.seed(_fake(hub).workspaces[env_id])
    raced.archive(env_id)
    hub.container.environment_override = raced

    reset = await hub.http.post(f"/v1/sessions/{session['id']}/workspace/reset", headers=bearer())

    assert reset.status_code == 200, reset.text
    assert raced.resets == [env_id]
    assert raced.contents[(env_id, "sessions/newcomer/progress.md")] == "theirs"


async def test_a_conflict_other_than_archival_never_resets_the_environment(hub: Hub) -> None:
    """A full quota is a 409 too. Resetting on it would wipe a workspace that was working."""

    class Full(FakeEnvironmentsClient):
        async def mkdir(self, environment_id: str, path: str) -> str:
            del environment_id, path
            raise ConflictError(SERVICE, 409, "disk quota exceeded", "quota-exceeded")

    session = await create(hub, key="full")
    full = Full()
    full.seed(_fake(hub).workspaces[str(session["workspace_environment_id"])])
    hub.container.environment_override = full

    with pytest.raises(ConflictError) as refused:
        await hub.container.reset_workspace(_pack_request(str(session["id"])), str(session["id"]))

    assert refused.value.code == "quota-exceeded"
    assert full.resets == []
