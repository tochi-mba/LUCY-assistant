"""Compaction, usage and stored results, over HTTP and against the store.

The interesting properties are not that the routes exist. They are that a summary never
rewrites the transcript, that three failed attempts stop asking, and that a `$hits` still
resolves after the turn that produced it has finished.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from weftai.results.types import StoredResult

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.core.errors import LucyError
from lucy_api.sessions.compact import (
    FAILURES_BEFORE_DISABLE,
    _clip,
    _covers_to,
    _operation,
    _summary,
    _text,
    compact_session,
    uncompact_session,
)
from lucy_api.sessions.models import CreateSession, Outcome
from lucy_api.sessions.results import _public, get_result, list_results, resolve_result
from lucy_api.sessions.sql_store import NewItem
from lucy_api.sessions.turns import close_turn, open_turn
from lucy_api.sessions.usage import session_usage
from lucy_api.store.results import SqlResultStore
from lucy_api.work.types import Brief, Kind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container
    from lucy_api.sessions.sql_store import SessionStore


@pytest.fixture
async def hub(
    settings: Settings, keyring: FakeKeyring
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
        yield http, container


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "econ-session")
    return str(created["id"])


async def n_turns(store: SessionStore, session: str, count: int) -> None:
    for index in range(count):
        turn = await open_turn(store, ACCOUNT, session, {"n": index})
        tid = str(turn["id"])
        await store.append(
            ACCOUNT,
            session,
            NewItem(
                "message",
                "user",
                f"Look at https://example.com/{index} and ses_{index} please " + ("word " * 80),
                turn=tid,
            ),
        )
        await store.append(
            ACCOUNT,
            session,
            NewItem("tool_result", "tool", {"op": "workspace.grep", "path": "a.py"}, turn=tid),
        )
        await close_turn(store, ACCOUNT, tid, Outcome("completed"))


async def three_turns(store: SessionStore, session: str) -> None:
    await n_turns(store, session, 3)


def test_extractive_helpers_keep_identifiers_and_confess_a_cut() -> None:
    assert _text("") == ""
    assert _text("{not json") == "{not json"
    assert _text('"quoted"') == "quoted"
    assert _text({"op": "notes.search"}) == '{"op": "notes.search"}'
    assert _text(4) == "4"
    assert _operation("{not json") == ""
    assert _operation("[1]") == ""
    assert _operation('{"name": "workspace.read"}') == "workspace.read"
    assert _operation("{}") == ""
    assert _clip("short", 20) == "short"
    assert _clip("a   b", 20) == "a b"
    assert _clip("abcdefghijklmnopqrstuvwxyz", 8).endswith("…")
    assert (
        _covers_to(
            [
                {"turn_id": "t1", "seq": 1},
                {"turn_id": "t2", "seq": 2},
                {"turn_id": "t3", "seq": 3},
            ],
            keep_recent=0,
        )
        == 3
    )
    assert (
        _covers_to(
            [
                {"turn_id": "t1", "seq": 1},
                {"turn_id": "t2", "seq": 2},
                {"turn_id": "t3", "seq": 3},
                {"turn_id": "t4", "seq": 4},
                {"turn_id": "t5", "seq": 5},
            ],
            keep_recent=4,
        )
        == 1
    )
    assert (
        _covers_to(
            [
                {"turn_id": "t1", "seq": 1},
                {"turn_id": "t2", "seq": 2},
                {"turn_id": "t3", "seq": 3},
            ]
        )
        == 1
    )
    assert (
        _covers_to(
            [
                {"turn_id": "t1", "seq": 0},
                {"turn_id": "t2", "seq": 1},
                {"turn_id": "t3", "seq": 2},
            ]
        )
        is None
    )
    duplicate = _summary(
        [
            {"seq": 1, "content_json": "see $hits and $hits again", "role": "assistant"},
            {"seq": 2, "content_json": "later", "role": "assistant"},
        ],
        covers_to=1,
    )
    assert duplicate.count("$hits") == 1
    assert "Covered items 1-1" in duplicate
    assert _text(None) == ""
    assert _text([1, 2]) == "[1, 2]"


async def test_compacting_an_empty_session_is_a_conflict(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    with pytest.raises(LucyError) as caught:
        await compact_session(sessions_store, ACCOUNT, session)
    assert caught.value.code == "conflict"
    assert "no items" in caught.value.args[0]


async def test_compacting_too_few_turns_is_a_conflict(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, ACCOUNT, session, {})
    await sessions_store.append(
        ACCOUNT, session, NewItem("message", "user", "hi", turn=str(turn["id"]))
    )
    await close_turn(sessions_store, ACCOUNT, str(turn["id"]), Outcome("completed"))
    with pytest.raises(LucyError) as caught:
        await compact_session(sessions_store, ACCOUNT, session)
    assert "newest" in caught.value.args[0]


async def test_a_compaction_is_extractive_and_can_be_undone(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    await three_turns(sessions_store, session)
    written = await compact_session(sessions_store, ACCOUNT, session)
    assert written["active"] is True
    assert "https://example.com/0" in written["summary"]
    assert "workspace.grep" in written["summary"]
    restored = await uncompact_session(sessions_store, ACCOUNT, session, str(written["id"]))
    assert restored["active"] is False
    with pytest.raises(LucyError) as caught:
        await uncompact_session(sessions_store, ACCOUNT, session, "cmp_missing")
    assert caught.value.code == "not-found"


async def test_three_failed_compactions_disable_the_session(
    sessions_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await a_session(sessions_store)
    await three_turns(sessions_store, session)

    def boom(*_args: object) -> str:
        raise LucyError("compact-failed", "the summariser could not run", 500)

    monkeypatch.setattr("lucy_api.sessions.compact._summary", boom)
    for _ in range(FAILURES_BEFORE_DISABLE):
        with pytest.raises(LucyError) as caught:
            await compact_session(sessions_store, ACCOUNT, session)
        assert caught.value.code == "compact-failed"
    with pytest.raises(LucyError) as caught:
        await compact_session(sessions_store, ACCOUNT, session)
    assert "disabled" in caught.value.args[0]


async def test_a_successful_compaction_resets_the_failure_streak(
    sessions_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await a_session(sessions_store)
    await three_turns(sessions_store, session)
    await compact_session(sessions_store, ACCOUNT, session)

    def boom(*_args: object) -> str:
        raise LucyError("compact-failed", "the summariser could not run", 500)

    monkeypatch.setattr("lucy_api.sessions.compact._summary", boom)
    with pytest.raises(LucyError):
        await compact_session(sessions_store, ACCOUNT, session)
    monkeypatch.undo()
    written = await compact_session(sessions_store, ACCOUNT, session)
    assert written["active"] is True


async def test_usage_reads_the_session_row_and_the_turn_sums(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    usage = await session_usage(sessions_store, ACCOUNT, session)
    assert usage["turns"] == 0
    assert usage["input_tokens"] == 0
    await three_turns(sessions_store, session)
    later = await session_usage(sessions_store, ACCOUNT, session)
    assert later["turns"] == 3


async def test_results_round_trip_through_list_get_and_ref(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    stored: StoredResult = {
        "id": "hits",
        "operation": "research.search",
        "kind": "collection",
        "type": "hits",
        "data": ({"url": "https://example.com", "title": "One"},),
        "items": ({"url": "https://example.com", "title": "One"},),
        "count": 1,
        "notices": ("showing 1 of 1",),
        "storedAt": 1.0,
    }

    def write(db: Any) -> None:
        SqlResultStore(db).set(session, stored)

    await sessions_store.worker.call(write)
    listed = await list_results(sessions_store, ACCOUNT, session)
    assert listed[0]["id"] == "hits"
    fetched = await get_result(sessions_store, ACCOUNT, session, "hits")
    assert fetched["notices"] == ["showing 1 of 1"]
    resolved = await resolve_result(sessions_store, ACCOUNT, session, "$hits")
    assert resolved["ref"] == "$hits"
    with pytest.raises(LucyError) as missing:
        await get_result(sessions_store, ACCOUNT, session, "absent")
    assert missing.value.code == "not-found"
    with pytest.raises(LucyError) as missing_ref:
        await resolve_result(sessions_store, ACCOUNT, session, "$gone")
    assert missing_ref.value.code == "not-found"
    with pytest.raises(LucyError) as bad:
        await resolve_result(sessions_store, ACCOUNT, session, "not-a-ref")
    assert bad.value.code == "bad-ref"


def test_a_pydantic_result_is_rendered_as_json_not_as_the_model() -> None:
    from pydantic import BaseModel

    class Hit(BaseModel):
        title: str

    rendered = _public({"hits": (Hit(title="One"),)})
    assert rendered == {"hits": [{"title": "One"}]}


async def test_economy_routes_are_session_scoped(
    hub: tuple[AsyncClient, Container],
) -> None:
    http, container = hub
    created = await http.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "econ-http"}
    )
    assert created.status_code == 201, created.text
    session = created.json()["id"]
    store = container.store
    empty = await http.post(f"/v1/sessions/{session}/compact", headers=bearer())
    assert empty.status_code == 409
    await n_turns(store, session, 3)
    too_new = await http.post(f"/v1/sessions/{session}/compact", headers=bearer())
    assert too_new.status_code == 409, too_new.text
    assert "4" in too_new.text
    await n_turns(store, session, 2)
    compacted = await http.post(f"/v1/sessions/{session}/compact", headers=bearer())
    assert compacted.status_code == 200, compacted.text
    undone = await http.post(
        f"/v1/sessions/{session}/uncompact",
        json={"id": compacted.json()["id"]},
        headers=bearer(),
    )
    assert undone.status_code == 200
    usage = await http.get(f"/v1/sessions/{session}/usage", headers=bearer())
    assert usage.status_code == 200
    assert usage.json()["turns"] == 5
    listed = await http.get(f"/v1/sessions/{session}/results", headers=bearer())
    assert listed.status_code == 200
    assert listed.json()["data"] == []
    missing = await http.get(f"/v1/sessions/{session}/results/nope", headers=bearer())
    assert missing.status_code == 404
    bad_ref = await http.get(
        f"/v1/sessions/{session}/results",
        params={"ref": "not-a-ref"},
        headers=bearer(),
    )
    assert bad_ref.status_code == 400
    bad = await http.get(
        f"/v1/sessions/{session}/results", params={"ref": "nope"}, headers=bearer()
    )
    assert bad.status_code == 400


async def test_listing_helpers_and_the_oauth_resource_document(
    hub: tuple[AsyncClient, Container],
) -> None:
    http, container = hub
    created = await http.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "econ-agents"}
    )
    session = created.json()["id"]

    async def hang() -> None:
        await asyncio.Event().wait()

    container.work.start(
        hang(),
        Brief(session_id=session, kind=Kind.helper, role="reviewer", objective="wait"),
    )
    for _ in range(4):
        await asyncio.sleep(0)
    listed = await http.get(f"/v1/sessions/{session}/agents", headers=bearer())
    assert listed.status_code == 200
    assert listed.json()["data"][0]["role"] == "reviewer"
    await container.work.shutdown()
    discovery = await http.get("/.well-known/oauth-protected-resource")
    assert discovery.status_code == 200
    body = discovery.json()
    assert body["bearer_methods_supported"] == ["header"]
    assert body["authorization_servers"]
