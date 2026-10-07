"""A new conversation starts with what its profile has been using bound.

The bug, named: recency was the conversation's alone and in memory, so every new session
started cold -- asked to play a song, Lucy spent a whole round binding music and reading its
page first, about fifteen thousand tokens and fifty seconds on the weakest model, every
session, for a person who plays music daily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from conftest import ACCOUNT

from lucy_api.packs.service import Capabilities
from lucy_api.sessions.habits import DAY_SECONDS, HABIT_DAYS, recent_capabilities
from lucy_api.sessions.models import CreateSession

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore

NOW = 1_800_000_000.0


async def a_session(store: SessionStore, key: str, **fields: Any) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo", **fields), key)
    return str(created["id"])


async def ran(store: SessionStore, session: str, turn: str, *steps: tuple[str, str]) -> None:
    plan = {"steps": [{"id": f"s{i}", "op": op} for i, (op, _status) in enumerate(steps)]}
    result = {"steps": [{"id": f"s{i}", "status": status} for i, (_op, status) in enumerate(steps)]}
    await store.record_steps(ACCOUNT, session, turn, plan, result)


async def at(store: SessionStore, session: str, when: float) -> None:
    def apply(db: Any) -> None:
        db.execute("UPDATE steps SET created_at=? WHERE session_id=?", (when, session))

    await store.transaction(apply)


async def test_a_profiles_recent_capabilities_come_newest_first(
    sessions_store: SessionStore,
) -> None:
    store = sessions_store
    older = await a_session(store, "habit-1", profile="home")
    await ran(store, older, "t1", ("notes.search", "ok"), ("research.search", "ok"))
    await at(store, older, NOW - 3 * DAY_SECONDS)
    newer = await a_session(store, "habit-2", profile="home")
    await ran(store, newer, "t2", ("music.play", "ok"), ("music.find", "ok"))
    await at(store, newer, NOW - DAY_SECONDS)

    found = await recent_capabilities(store, ACCOUNT, "home", now=NOW)

    assert found[0] == "music", "the newest habit first"
    assert set(found) == {"music", "notes", "research"}


async def test_what_does_not_say_something_about_the_profile_is_left_out(
    sessions_store: SessionStore,
) -> None:
    store = sessions_store
    off_record = await a_session(store, "habit-3", profile="home", incognito=True)
    await ran(store, off_record, "t3", ("music.play", "ok"))
    elsewhere = await a_session(store, "habit-4", profile="work")
    await ran(store, elsewhere, "t4", ("repos.read", "ok"))
    failed = await a_session(store, "habit-5", profile="home")
    await ran(store, failed, "t5", ("research.search", "error"), ("tool", "ok"))
    for session in (off_record, elsewhere, failed):
        await at(store, session, NOW - DAY_SECONDS)
    stale = await a_session(store, "habit-6", profile="home")
    await ran(store, stale, "t6", ("workspace.read", "ok"))
    await at(store, stale, NOW - (HABIT_DAYS + 1) * DAY_SECONDS)

    assert await recent_capabilities(store, ACCOUNT, "home", now=NOW) == ()


async def test_a_conversations_own_recency_is_never_replaced_by_its_profiles() -> None:
    asked: list[str] = []

    async def habits() -> tuple[str, ...]:
        asked.append("once")
        return ("music", "notes", "music")

    capabilities = Capabilities(())
    await capabilities.seed_recent("ses_new", habits)
    await capabilities.seed_recent("ses_new", habits)
    assert capabilities.recent("ses_new") == ("music", "notes")
    assert asked == ["once"], "a conversation's habits are read once, not every turn"

    capabilities.remember_use("ses_used", "workspace")
    await capabilities.seed_recent("ses_used", habits)
    assert capabilities.recent("ses_used") == ("workspace",)
