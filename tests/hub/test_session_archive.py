"""Archiving: a quiet conversation leaves the list's front, and nothing else about it changes.

Split from `test_session_store.py`, which had grown past the family's thousand-line limit.
"""

from __future__ import annotations

import sqlite3
import time

import pytest
from test_session_store import OWNER, STRANGER, a_session, a_turn

from lucy_api.sessions.sql_store import SessionStore


@pytest.fixture
def store(sessions_store: SessionStore) -> SessionStore:
    """The conftest store, under the name these tests were written against."""
    return sessions_store


async def test_idle_conversations_are_archived_and_live_ones_are_not(
    store: SessionStore,
) -> None:
    """Quiet means no live turn, and zero days means never. A stranger's call is a no-op."""
    now = 1_800_000_000.0
    idle = await a_session(store)
    parked = await a_session(store)
    await a_turn(store, parked, status="input_required")
    foreign = await a_session(store, STRANGER)

    def age(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE sessions SET updated_at=? WHERE id IN (?,?,?)",
            (now - 2 * 86_400, idle, parked, foreign),
        )

    await store.transaction(age)

    assert await store.archive_idle(OWNER, days=0, now=now) == 0
    assert (await store.get(OWNER, idle))["archived_at"] is None
    assert await store.archive_idle(OWNER, days=1, now=now) == 1
    assert (await store.get(OWNER, idle))["archived_at"] == now
    assert (await store.get(OWNER, parked))["archived_at"] is None
    assert await store.archive_idle(STRANGER, days=1, now=now) == 1
    assert (await store.get(OWNER, idle))["archived_at"] == now
    listed = await store.list_sessions(OWNER, 50, None, None, "asc")
    assert {row["id"] for row in listed["data"]} >= {idle, parked}


async def test_archiving_one_profile_leaves_the_others_alone(store: SessionStore) -> None:
    now = time.time()
    work = await a_session(store, profile="work")
    home = await a_session(store, profile="personal")

    def age(db: sqlite3.Connection) -> None:
        db.execute("UPDATE sessions SET updated_at=?", (now - 2 * 86_400,))

    await store.transaction(age)

    assert await store.live_profiles(OWNER) == ("personal", "work")
    assert await store.archive_idle(OWNER, profile="work", days=1, now=now) == 1
    assert (await store.get(OWNER, work))["archived_at"] == now
    assert (await store.get(OWNER, home))["archived_at"] is None
    assert await store.live_profiles(OWNER) == ("personal",)
    assert await store.live_profiles(STRANGER) == ()


async def test_archiving_and_unarchiving_move_a_timestamp_rather_than_a_flag(
    store: SessionStore,
) -> None:
    session = await a_session(store)

    archived = await store.update(OWNER, session, {"archived": True})
    assert archived["archived_at"] is not None

    restored = await store.update(OWNER, session, {"archived": False})
    assert restored["archived_at"] is None
