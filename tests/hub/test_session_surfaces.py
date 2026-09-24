"""Session memory, workspace and helper roster as HTTP, not as another store."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from test_sessions_api import Hub, create

from lucy_api.agents.store import AgentStore
from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.memory import TopicCard
from lucy_api.sessions.scope import PROGRESS_FILE
from lucy_api.sessions.sql_store import NewItem

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings

OTHER = "acct_someone_else"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


class Listing:
    def __init__(self, cards: tuple[TopicCard, ...]) -> None:
        self.cards = cards

    async def topics(self, *, profile: str = "") -> tuple[TopicCard, ...]:
        del profile
        return self.cards


class BoomListing:
    async def topics(self, *, profile: str = "") -> tuple[TopicCard, ...]:
        del profile
        raise DownstreamError("memory-api", 503, "offline")


def card(**overrides: object) -> TopicCard:
    fields: dict[str, object] = {
        "id": "t-ok",
        "key": "tea",
        "title": "Tea",
        "summary": "How they take it",
        "count": 2,
        "importance": 0.4,
        "trust": "stated",
        "last_seen": NOW,
    }
    fields.update(overrides)
    return TopicCard(**fields)  # type: ignore[arg-type]


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


async def test_session_memory_is_the_trusted_topic_index(hub: Hub) -> None:
    session = await create(hub, key="mem-1")
    hub.container.memory_topics = Listing(
        (
            card(),
            card(id="t-inject", title="Ignore previous", trust="untrusted", importance=1.0),
        )
    )
    shown = await hub.http.get(f"/v1/sessions/{session['id']}/memory", headers=bearer())
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert body["incognito"] is False
    assert [row["id"] for row in body["data"]] == ["t-ok"]
    assert body["data"][0]["last_seen"] == NOW.isoformat()


async def test_incognito_memory_is_empty_rather_than_a_differently_shaped_miss(
    hub: Hub,
) -> None:
    session = await create(hub, key="mem-incog", incognito=True)
    hub.container.memory_topics = Listing((card(),))
    shown = await hub.http.get(f"/v1/sessions/{session['id']}/memory", headers=bearer())
    assert shown.status_code == 200
    assert shown.json() == {"data": [], "incognito": True}


async def test_a_memory_outage_is_a_notice_not_a_500(hub: Hub) -> None:
    session = await create(hub, key="mem-down")
    hub.container.memory_topics = BoomListing()
    shown = await hub.http.get(f"/v1/sessions/{session['id']}/memory", headers=bearer())
    assert shown.status_code == 200
    body = shown.json()
    assert body["data"] == []
    assert body["notice"] == "memory is unavailable"


async def test_an_unset_listing_uses_the_http_client_and_still_degrades(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await create(hub, key="mem-http")

    class BoomClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def topics(self, *, profile: str = "") -> tuple[TopicCard, ...]:
            del profile
            raise DownstreamError("memory-api", 503, "offline")

    monkeypatch.setattr("lucy_api.core.container.HttpMemoryClient", BoomClient)
    hub.container.memory_topics = None
    shown = await hub.http.get(f"/v1/sessions/{session['id']}/memory", headers=bearer())
    assert shown.status_code == 200
    assert shown.json()["notice"] == "memory is unavailable"


async def test_a_new_session_already_has_a_confined_workspace(hub: Hub) -> None:
    session = await create(hub, key="ws-1")
    viewed = await hub.http.get(f"/v1/sessions/{session['id']}/workspace", headers=bearer())
    assert viewed.status_code == 200
    body = viewed.json()
    assert body["status"] == "attached"
    assert body["environment_id"] == session["workspace_environment_id"]
    assert body["path"] == session["workspace_rel"]
    assert "home" not in str(body).lower()
    assert hub.container.workspace_view({}) == {
        "environment_id": None,
        "path": "",
        "status": "missing",
    }
    attached = await hub.http.post(f"/v1/sessions/{session['id']}/workspace", headers=bearer())
    assert attached.status_code == 200
    assert attached.json()["environment_id"] == body["environment_id"]


async def test_resetting_a_workspace_rewrites_the_journal_and_drops_scratch(
    hub: Hub,
) -> None:
    session = await create(hub, key="ws-reset")
    fake = hub.container.environment_override
    assert isinstance(fake, FakeEnvironmentsClient)
    env_id = str(session["workspace_environment_id"])
    rel = str(session["workspace_rel"])
    scratch = f"{rel}/scratch.txt"
    await fake.write(env_id, scratch, "temp")

    async def gone(environment_id: str, path: str, *, recursive: bool = False) -> None:
        del environment_id, path, recursive
        raise KeyError("gone")

    fake.delete = gone  # type: ignore[method-assign]
    reset = await hub.http.post(f"/v1/sessions/{session['id']}/workspace/reset", headers=bearer())
    assert reset.status_code == 200
    assert reset.json()["status"] == "attached"
    names = [path for workspace, path in fake.contents if workspace == env_id]
    assert any(path.endswith(PROGRESS_FILE) for path in names)

    def clear(db: Any) -> None:
        db.execute(
            "UPDATE sessions SET workspace_environment_id=NULL, workspace_rel='' WHERE id=?",
            (session["id"],),
        )

    await hub.store.transaction(clear)
    restored = await hub.http.post(
        f"/v1/sessions/{session['id']}/workspace/reset", headers=bearer()
    )
    assert restored.status_code == 200
    assert restored.json()["status"] == "attached"

    def drop_rel(db: Any) -> None:
        db.execute("UPDATE sessions SET workspace_rel='' WHERE id=?", (session["id"],))

    await hub.store.transaction(drop_rel)
    repaired = await hub.http.post(
        f"/v1/sessions/{session['id']}/workspace/reset", headers=bearer()
    )
    assert repaired.status_code == 200
    assert repaired.json()["status"] == "attached"
    assert repaired.json()["path"]


async def test_helper_items_are_not_in_the_parent_transcript(hub: Hub) -> None:
    session = await create(hub, key="agt-1")
    agents = AgentStore(hub.store)
    agent_id = await agents.insert(
        ACCOUNT, session["id"], role="reviewer", objective="read the brief", depth=1
    )
    await hub.store.append(ACCOUNT, session["id"], NewItem("message", "user", {"text": "parent"}))
    await hub.store.append(
        ACCOUNT,
        session["id"],
        NewItem("message", "assistant", {"text": "child"}, agent_id=agent_id),
    )
    parent = await hub.http.get(f"/v1/sessions/{session['id']}/items", headers=bearer())
    assert parent.status_code == 200
    assert all(row.get("agent_id") in {None, ""} for row in parent.json()["data"])
    roster = await hub.http.get(f"/v1/sessions/{session['id']}/subagents", headers=bearer())
    assert roster.status_code == 200
    assert roster.json()["data"][0]["id"] == agent_id
    one = await hub.http.get(f"/v1/sessions/{session['id']}/subagents/{agent_id}", headers=bearer())
    assert one.status_code == 200
    assert one.json()["role"] == "reviewer"
    items = await hub.http.get(
        f"/v1/sessions/{session['id']}/subagents/{agent_id}/items", headers=bearer()
    )
    assert items.status_code == 200
    assert items.json()["data"][0]["agent_id"] == agent_id
    turns = await hub.http.get(
        f"/v1/sessions/{session['id']}/subagents/{agent_id}/turns", headers=bearer()
    )
    assert turns.status_code == 200
    assert turns.json()["data"] == []
    other = await create(hub, key="agt-other")
    foreign = await hub.http.get(
        f"/v1/sessions/{other['id']}/subagents/{agent_id}", headers=bearer()
    )
    assert foreign.status_code == 404
    stolen = await hub.http.get(
        f"/v1/sessions/{session['id']}/subagents/{agent_id}",
        headers=bearer(account_id=OTHER),
    )
    assert stolen.status_code == 404
