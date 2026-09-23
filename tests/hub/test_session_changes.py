"""Changing a session while it is working: warn, apply now, or hold until the turn ends.

The store half is transactional and the request half is one function, so both are driven
directly. The properties: a title changes whenever; a behaviour change against a live turn
is a warning unless the caller says which way; "now" lands on the running turn and says so;
"after_turn" is held, visible, merged, and spent exactly once when the turn ends.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.api.schemas.problem import PROBLEM_CONTENT_TYPE
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.core.container import after_turn
from lucy_api.core.errors import LucyError
from lucy_api.sessions.changes import APPLY_AFTER, APPLY_NOW, change_session, warning
from lucy_api.sessions.models import CreateSession, UpdateSession
from lucy_api.sessions.schema import ADDED_COLUMNS
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.stream.events import SESSION_CHANGE_HELD, SESSION_UPDATED
from lucy_api.work import Waker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings

OLD_SESSIONS_TABLE = """
CREATE TABLE sessions (
 id TEXT PRIMARY KEY, account_id TEXT NOT NULL, profile TEXT NOT NULL,
 title TEXT NOT NULL, status TEXT NOT NULL, model TEXT NOT NULL,
 thinking_config TEXT NOT NULL, persona TEXT NOT NULL, parent_session_id TEXT,
 forked_from_item TEXT, workspace_environment_id TEXT, workspace_rel TEXT,
 harness_version TEXT NOT NULL, input_policy TEXT NOT NULL, durability_mode TEXT NOT NULL,
 permission_mode TEXT NOT NULL, incognito INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, archived_at REAL,
 input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
 cost_micros INTEGER NOT NULL DEFAULT 0
) STRICT;
"""
"""The sessions table as it was before the two columns, for the migration test."""


class Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore, **fields: Any) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo", **fields), "key")
    return str(created["id"])


async def a_live_turn(store: SessionStore, session: str, key: str = "k1") -> str:
    turn = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], key
    )
    return str(turn["id"])


async def events_of(store: SessionStore, session: str) -> list[tuple[str, dict[str, Any]]]:
    rows = await store.records(ACCOUNT, session, "events")
    return [(str(row["type"]), dict(row["data"])) for row in rows]


# --------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------


async def test_a_database_opened_before_the_columns_existed_gains_them_on_initialize(
    tmp_path: Any,
) -> None:
    path = str(tmp_path / "old.sqlite3")
    with sqlite3.connect(path) as db:
        db.executescript(OLD_SESSIONS_TABLE)
    worker = SqlWorker(path)
    try:
        await SessionStore(worker).initialize()
        columns = await worker.call(
            lambda db: {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        )
        assert {column for _, column, _ in ADDED_COLUMNS} <= columns
        await SessionStore(worker).initialize()
    finally:
        await worker.aclose()


async def test_a_session_is_created_with_its_own_disabled_list_cleaned(
    store: SessionStore,
) -> None:
    session = await a_session(store, disabled_capabilities=[" agents", "music", "agents", " "])

    row = await store.get(ACCOUNT, session)

    assert row["disabled_capabilities"] == ["agents", "music"]
    assert row["pending_changes"] is None


async def test_live_turn_is_the_working_turn_or_nothing(store: SessionStore) -> None:
    session = await a_session(store)
    assert await store.live_turn(ACCOUNT, session) is None

    turn = await a_live_turn(store, session)
    assert await store.live_turn(ACCOUNT, session) == turn

    await store.finish_turn(ACCOUNT, turn, "completed")
    assert await store.live_turn(ACCOUNT, session) is None


async def test_held_changes_merge_and_are_spent_once(store: SessionStore) -> None:
    session = await a_session(store)

    first = await store.hold_changes(ACCOUNT, session, {"permission_mode": "plan"})
    second = await store.hold_changes(ACCOUNT, session, {"disabled_capabilities": ["agents"]})
    assert first["pending_changes"] == {"permission_mode": "plan"}
    assert second["pending_changes"] == {
        "permission_mode": "plan",
        "disabled_capabilities": ["agents"],
    }
    assert second["permission_mode"] == "ask", "held, not applied"

    applied = await store.apply_pending(ACCOUNT, session)
    assert applied is not None
    assert applied["permission_mode"] == "plan"
    assert applied["disabled_capabilities"] == ["agents"]
    assert applied["pending_changes"] is None
    assert await store.apply_pending(ACCOUNT, session) is None, "spent once"

    types = [kind for kind, _ in await events_of(store, session)]
    assert types.count(SESSION_CHANGE_HELD) == 2
    updated = [data for kind, data in await events_of(store, session) if kind == SESSION_UPDATED]
    assert updated[-1] == {
        "permission_mode": "plan",
        "disabled_capabilities": ["agents"],
        "held": True,
    }


# --------------------------------------------------------------------------------------
# The request
# --------------------------------------------------------------------------------------


async def test_an_idle_session_takes_a_behaviour_change_at_once(store: SessionStore) -> None:
    session = await a_session(store)

    row = await change_session(
        store,
        ACCOUNT,
        session,
        UpdateSession(permission_mode="plan", disabled_capabilities=["agents"]),
    )

    assert row["permission_mode"] == "plan"
    assert row["disabled_capabilities"] == ["agents"]
    assert row["pending_changes"] is None


async def test_a_title_changes_during_a_turn_without_a_warning(store: SessionStore) -> None:
    session = await a_session(store)
    await a_live_turn(store, session)

    row = await change_session(store, ACCOUNT, session, UpdateSession(title="Renamed"))

    assert row["title"] == "Renamed"


async def test_a_behaviour_change_during_a_turn_is_a_warning_that_names_both_answers(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_live_turn(store, session)

    with pytest.raises(LucyError) as raised:
        await change_session(
            store,
            ACCOUNT,
            session,
            UpdateSession(permission_mode="auto", disabled_capabilities=["music"]),
        )

    assert raised.value.status == 409
    assert str(raised.value) == warning(turn, ["disabled_capabilities", "permission_mode"])
    assert turn in str(raised.value)
    assert f"apply='{APPLY_NOW}'" in str(raised.value)
    assert f"apply='{APPLY_AFTER}'" in str(raised.value)
    assert (await store.get(ACCOUNT, session))["permission_mode"] == "ask", "nothing changed"


async def test_apply_now_lands_on_the_running_turn_and_the_event_says_which(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_live_turn(store, session)

    row = await change_session(
        store, ACCOUNT, session, UpdateSession(permission_mode="plan", apply="now")
    )

    assert row["permission_mode"] == "plan"
    kind, data = (await events_of(store, session))[-1]
    assert kind == SESSION_UPDATED
    assert data == {"permission_mode": "plan", "during_turn": turn}


async def test_apply_after_turn_holds_the_change_and_the_end_of_turn_hook_spends_it(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_live_turn(store, session)
    events = EventEmitter(SqlEventLog(store), Snapshot())
    waker = Waker(store, events)
    notified: list[str] = []

    async def notify(account: str, session_id: str, turn_id: str, status: str) -> None:
        notified.append(status)

    held = await change_session(
        store, ACCOUNT, session, UpdateSession(disabled_capabilities=["agents"], apply="after_turn")
    )
    assert held["pending_changes"] == {"disabled_capabilities": ["agents"]}
    assert held["disabled_capabilities"] == []

    ended = after_turn(notify, waker, store)
    await ended(ACCOUNT, session, turn, "input_required")
    assert (await store.get(ACCOUNT, session))["pending_changes"] is not None, "parked, not ended"

    await store.finish_turn(ACCOUNT, turn, "completed")
    await ended(ACCOUNT, session, turn, "completed")
    row = await store.get(ACCOUNT, session)
    assert row["pending_changes"] is None
    assert row["disabled_capabilities"] == ["agents"]
    assert notified == ["input_required", "completed"]


async def test_the_end_of_turn_hook_survives_a_session_that_was_deleted(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_live_turn(store, session)
    await store.finish_turn(ACCOUNT, turn, "completed")
    await store.delete(ACCOUNT, session)

    async def notify(*_: str) -> None:
        return None

    await after_turn(notify, Waker(store, EventEmitter(SqlEventLog(store), Snapshot())), store)(
        ACCOUNT, session, turn, "completed"
    )


# --------------------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------------------


@pytest.fixture
async def http(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[tuple[AsyncClient, Any]]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.environment_override = FakeEnvironmentsClient()
        yield client, container


async def test_over_http_the_warning_is_a_problem_and_the_answers_are_accepted(
    http: tuple[AsyncClient, Any],
) -> None:
    client, container = http
    created = await client.post(
        "/v1/sessions",
        json={"disabled_capabilities": ["music"]},
        headers={**bearer(), "Idempotency-Key": "k"},
    )
    assert created.status_code == 201, created.text
    session = created.json()["id"]
    assert created.json()["disabled_capabilities"] == ["music"]
    assert created.json()["pending_changes"] is None
    turn = await a_live_turn(container.store, session)

    warned = await client.patch(
        f"/v1/sessions/{session}", json={"disabled_capabilities": ["agents"]}, headers=bearer()
    )
    assert warned.status_code == 409
    assert warned.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert turn in warned.json()["detail"]

    held = await client.patch(
        f"/v1/sessions/{session}",
        json={"disabled_capabilities": ["agents"], "apply": "after_turn"},
        headers=bearer(),
    )
    assert held.status_code == 200
    assert held.json()["pending_changes"] == {"disabled_capabilities": ["agents"]}
    assert held.json()["disabled_capabilities"] == ["music"]

    now = await client.patch(
        f"/v1/sessions/{session}",
        json={"permission_mode": "plan", "apply": "now"},
        headers=bearer(),
    )
    assert now.status_code == 200
    assert now.json()["permission_mode"] == "plan"

    wrong = await client.patch(f"/v1/sessions/{session}", json={"apply": "later"}, headers=bearer())
    assert wrong.status_code == 422
