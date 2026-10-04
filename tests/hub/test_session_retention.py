"""`lucy.delete_archived_sessions_after_days`: archived conversations deleted, and only those.

Deleting cannot be undone, so these are mostly tests of what is *kept*: a conversation not
archived, archived too recently, written to since, or with anything still going, and the
day exactly on the boundary in both directions. A real database throughout, because the
claim is about one SQL statement and one transaction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.retention import DAY_SECONDS, SWEEP_LIMIT, delete_archived
from lucy_api.sessions.sql_store import NewItem, SessionStore, encoded, identifier
from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator
    from pathlib import Path

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container

OWNER = "acct_owner"
STRANGER = "acct_stranger"
NOW = 1_900_000_000.0
DAYS = 30
CUTOFF = NOW - DAYS * DAY_SECONDS


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def archived(
    store: SessionStore,
    *,
    archived_at: float | None = CUTOFF,
    updated_at: float = CUTOFF,
    account: str = OWNER,
) -> str:
    """A session with its archive stamp and last touch set to exactly what a test needs."""
    created = await store.create(account, CreateSession(), identifier("key"))
    session = str(created["id"])

    def stamp(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE sessions SET archived_at=?, updated_at=? WHERE id=?",
            (archived_at, updated_at, session),
        )

    await store.transaction(stamp)
    return session


async def write(store: SessionStore, sql: str, *values: object) -> None:
    def apply(db: sqlite3.Connection) -> None:
        db.execute(sql, values)

    await store.transaction(apply)


async def present(store: SessionStore, session: str) -> bool:
    def read(db: sqlite3.Connection) -> bool:
        return db.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone() is not None

    return bool(await store.worker.call(read))


async def test_zero_days_deletes_nothing_however_old_the_archive(store: SessionStore) -> None:
    """The default, and the outage value: archived conversations are kept for ever."""
    ancient = await archived(store, archived_at=1.0, updated_at=1.0)

    assert await delete_archived(store, OWNER, days=0, now=NOW) == ()
    assert await present(store, ancient)


async def test_the_boundary_day_is_deleted_and_one_second_younger_is_kept(
    store: SessionStore,
) -> None:
    """Archived and untouched for exactly the chosen days is old enough; a second less on
    either clock is not."""
    on_the_day = await archived(store)
    archived_later = await archived(store, archived_at=CUTOFF + 1)
    touched_later = await archived(store, updated_at=CUTOFF + 1)

    gone = await delete_archived(store, OWNER, days=DAYS, now=NOW)

    assert [row["id"] for row in gone] == [on_the_day]
    assert not await present(store, on_the_day)
    assert await present(store, archived_later)
    assert await present(store, touched_later)


async def test_an_unarchived_conversation_is_never_deleted_however_quiet(
    store: SessionStore,
) -> None:
    """Retention reads the archive stamp, not idleness: archiving is its own setting."""
    quiet = await archived(store, archived_at=None, updated_at=1.0)

    assert await delete_archived(store, OWNER, days=1, now=NOW) == ()
    assert await present(store, quiet)


@pytest.mark.parametrize("status", ["queued", "running", "input_required", "auth_required"])
async def test_a_conversation_with_a_turn_still_going_is_kept(
    store: SessionStore, status: str
) -> None:
    session = await archived(store)
    await write(
        store,
        "INSERT INTO turns (id,session_id,status,input_json,created_at) VALUES (?,?,?,?,?)",
        identifier("trn"),
        session,
        status,
        encoded({}),
        CUTOFF,
    )

    assert await delete_archived(store, OWNER, days=DAYS, now=NOW) == ()
    assert await present(store, session)


async def test_a_conversation_whose_turns_all_finished_is_deleted_with_its_transcript(
    store: SessionStore,
) -> None:
    """Items, turns and events go with the row; the audit says why it went."""
    session = await archived(store)
    await store.append(OWNER, session, NewItem("message", "user", "old news"))
    await write(
        store,
        "INSERT INTO turns (id,session_id,status,input_json,created_at) VALUES (?,?,?,?,?)",
        identifier("trn"),
        session,
        "completed",
        encoded({}),
        CUTOFF,
    )
    await write(store, "UPDATE sessions SET updated_at=? WHERE id=?", CUTOFF, session)

    gone = await delete_archived(store, OWNER, days=DAYS, now=NOW)

    assert [row["id"] for row in gone] == [session]

    def leftovers(db: sqlite3.Connection) -> int:
        queries = (
            "SELECT COUNT(*) FROM items WHERE session_id=?",
            "SELECT COUNT(*) FROM turns WHERE session_id=?",
            "SELECT COUNT(*) FROM events WHERE session_id=?",
        )
        return sum(int(db.execute(query, (session,)).fetchone()[0]) for query in queries)

    assert await store.worker.call(leftovers) == 0
    audit = await store.audit_log(OWNER)
    assert audit[-1]["action"] == "session.deleted"
    assert audit[-1]["detail"] == {"reason": "archived_retention", "days": DAYS}


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        (
            "agents",
            "INSERT INTO agents (id,session_id,role,objective,delegation_json,status,depth,"
            "tools_json,budget_json,created_at) VALUES (?,?,'helper','x','{}','running',1,"
            "'[]','{}',0)",
        ),
        (
            "subscriptions",
            "INSERT INTO subscriptions (id,session_id,account_id,profile,work_id,capability,"
            "secret,objective,wake,state,created_at,expires_at) VALUES (?,?,'acct_owner',"
            "'personal','w','watch','s','x',1,'running',0,0)",
        ),
    ],
)
async def test_a_running_helper_or_a_waiting_watch_keeps_its_conversation(
    store: SessionStore, table: str, sql: str
) -> None:
    del table
    session = await archived(store)
    await write(store, sql, identifier("row"), session)

    assert await delete_archived(store, OWNER, days=DAYS, now=NOW) == ()
    assert await present(store, session)


async def test_one_sweep_is_bounded_and_takes_the_oldest_archive_first(
    store: SessionStore,
) -> None:
    """A backlog is caught up over later listings, oldest first, never in one request."""
    sessions = [
        await archived(store, archived_at=CUTOFF - index) for index in range(SWEEP_LIMIT + 2)
    ]

    first = await delete_archived(store, OWNER, days=DAYS, now=NOW)
    second = await delete_archived(store, OWNER, days=DAYS, now=NOW)

    oldest_first = list(reversed(sessions))
    assert [row["id"] for row in first] == oldest_first[:SWEEP_LIMIT]
    assert [row["id"] for row in second] == oldest_first[SWEEP_LIMIT:]
    assert await delete_archived(store, OWNER, days=DAYS, now=NOW) == ()


async def test_a_sweep_touches_only_its_own_account(store: SessionStore) -> None:
    theirs = await archived(store, account=STRANGER)

    assert await delete_archived(store, OWNER, days=DAYS, now=NOW) == ()
    assert await present(store, theirs)


async def test_a_deleted_conversation_s_artifact_files_are_removed_from_disk(
    store: SessionStore, tmp_path: Path
) -> None:
    session = await archived(store)
    kept = await archived(store, archived_at=None)
    files = {name: tmp_path / f"{name}.bin" for name in ("gone", "kept")}
    for (name, path), owner in zip(files.items(), (session, kept), strict=True):
        path.write_bytes(b"artifact")
        await write(
            store,
            "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)",
            identifier(name),
            owner,
            str(path),
            8,
            "application/octet-stream",
            "test",
            CUTOFF,
        )

    await delete_archived(store, OWNER, days=DAYS, now=NOW)

    assert not files["gone"].exists()
    assert files["kept"].exists()


@dataclass(frozen=True, slots=True)
class Hub:
    http: AsyncClient
    container: Container


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
        yield Hub(http=http, container=container)


async def _create(hub: Hub, key: str) -> dict[str, Any]:
    response = await hub.http.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": key}
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def test_listing_deletes_expired_archives_and_their_workspace_folders(hub: Hub) -> None:
    """With the setting on, a listing deletes what has expired before it pages, and the
    sandbox loses that conversation's folder; a recent archive is listed as before."""
    expired = await _create(hub, "expired")
    recent = await _create(hub, "recent")
    env = hub.container.environment_override
    assert env is not None
    env_id = str(expired["workspace_environment_id"])
    rel = str(expired["workspace_rel"])
    env.contents[(env_id, f"{rel}/notes.md")] = "old"
    old = time.time() - 100 * DAY_SECONDS
    for session, stamp in ((expired["id"], old), (recent["id"], time.time())):
        await write(
            hub.container.store,
            "UPDATE sessions SET archived_at=?, updated_at=? WHERE id=?",
            stamp,
            stamp,
            session,
        )
    hub.container.preferences.seed("lucy", {"delete_archived_sessions_after_days": 90})

    listed = await hub.http.get("/v1/sessions", headers=bearer())

    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["data"]] == [recent["id"]]
    assert (env_id, f"{rel}/notes.md") not in env.contents
    gone = await hub.http.get(f"/v1/sessions/{expired['id']}", headers=bearer())
    assert gone.status_code == 404


async def test_listing_with_nothing_chosen_keeps_every_archive(hub: Hub) -> None:
    """The default leaves listing exactly as it was: archived, listed, not deleted."""
    created = await _create(hub, "kept")
    old = time.time() - 4_000 * DAY_SECONDS
    await write(
        hub.container.store,
        "UPDATE sessions SET archived_at=?, updated_at=? WHERE id=?",
        old,
        old,
        created["id"],
    )

    listed = await hub.http.get("/v1/sessions", headers=bearer())

    assert [row["id"] for row in listed.json()["data"]] == [created["id"]]
