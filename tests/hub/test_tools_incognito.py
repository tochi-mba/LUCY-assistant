"""A tool call scoped to an incognito session is incognito, whoever makes it.

`GET /v1/tools` and `POST /v1/tools/{name}/invoke` built their pack context from the
session's profile and permission mode and dropped its incognito flag. So `notes.remember`
invoked in an incognito session wrote a memory, and `notes.search` read the person's -- the
one thing an incognito session promises not to do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.clients.memory import Note
from lucy_api.packs.notes import INCOGNITO

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.clients.memory import Block, Draft
    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container
    from lucy_api.packs.context import PackContext


class Memory:
    """Just enough of the memory service for notes to be bound, recording every write."""

    def __init__(self) -> None:
        self.written: list[Draft] = []

    async def blocks(self, *, profile: str = "") -> tuple[Block, ...]:
        del profile
        return ()

    async def remember(self, draft: Draft) -> Note:
        self.written.append(draft)
        return Note(id="mem_1", title=draft.title, body=draft.body)


@pytest.fixture
def memory() -> Memory:
    return Memory()


@pytest.fixture
async def hub(
    settings: Settings, keyring: FakeKeyring, memory: Memory
) -> AsyncIterator[tuple[AsyncClient, Container]]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.environment_override = FakeEnvironmentsClient()
        for pack in container.capabilities.packs:
            if pack.id == "notes":
                pack._override = memory
        yield http, container


async def _session(http: AsyncClient, *, incognito: bool) -> str:
    created = await http.post(
        "/v1/sessions",
        json={"incognito": incognito, "permission_mode": "auto"},
        headers={**bearer(), "Idempotency-Key": f"incognito-{incognito}"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def _invoke(http: AsyncClient, name: str, arguments: dict[str, str], session: str) -> Any:
    answer = await http.post(
        f"/v1/tools/{name}/invoke",
        json={"input": arguments, "session_id": session},
        headers=bearer(),
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["steps"][0]


def _watch(container: Container) -> list[PackContext]:
    """Every pack context the routes probe with, in order."""
    seen: list[PackContext] = []
    probe = container.capabilities.probe

    async def probed(context: PackContext) -> Any:
        seen.append(context)
        return await probe(context)

    container.capabilities.probe = probed  # type: ignore[method-assign]
    return seen


@pytest.mark.parametrize("incognito", [True, False])
async def test_a_memory_write_invoked_in_an_incognito_session_writes_nothing(
    hub: tuple[AsyncClient, Container], memory: Memory, incognito: bool
) -> None:
    """The bug, named: this call reached the memory service. The other case shows it can."""
    http, _container = hub
    session = await _session(http, incognito=incognito)
    await _invoke(http, "capabilities.use", {"id": "notes"}, session)

    step = await _invoke(http, "notes.remember", {"title": "tea", "body": "green"}, session)

    if incognito:
        assert step["data"] == {"status": "incognito", "message": INCOGNITO}
        assert memory.written == []
    else:
        assert [draft.title for draft in memory.written] == ["tea"]


@pytest.mark.parametrize("incognito", [True, False])
async def test_both_routes_carry_the_session_s_incognito_flag(
    hub: tuple[AsyncClient, Container], incognito: bool
) -> None:
    http, container = hub
    session = await _session(http, incognito=incognito)
    seen = _watch(container)

    listed = await http.get("/v1/tools", params={"session_id": session}, headers=bearer())
    await _invoke(http, "help.docs", {"topic": "notes"}, session)

    assert listed.status_code == 200, listed.text
    assert len(seen) == 2
    assert all(context.incognito is incognito for context in seen)


async def test_a_call_scoped_to_no_session_is_not_incognito(
    hub: tuple[AsyncClient, Container],
) -> None:
    http, container = hub
    seen = _watch(container)
    listed = await http.get("/v1/tools", headers=bearer())
    assert listed.status_code == 200, listed.text
    assert [context.incognito for context in seen] == [False]
