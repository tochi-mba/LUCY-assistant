"""A group of work wakes the session once, when its last member ends.

Five reviewers started as one team, each waking the session on its own ending, opened turns
that each knew a fifth of the news. So a member of a group wakes nothing itself; the group's
ending is one event, one harness line naming every member, and at most one turn.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.stream.events import WORK_FINISHED, WORK_GROUP_FINISHED, WORK_WOKE
from lucy_api.work import Kind, Record, State, Team, Waker
from lucy_api.work.wake import NOTICE_KIND, WAKE_INPUT, team_wake_line

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_groups"
START = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


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


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    return str(created["id"])


def a_member(session_id: str, identifier: str, **overrides: Any) -> Record:
    fields: dict[str, Any] = {
        "id": identifier,
        "kind": Kind.helper,
        "role": "reviewer",
        "objective": "Read the draft through one lens",
        "session_id": session_id,
        "started_at": START,
        "finished_at": START + timedelta(seconds=60),
        "state": State.succeeded,
        "account_id": ACCOUNT,
        "wake": True,
        "tokens": 300,
        "group": "reviewers",
    }
    return Record(**{**fields, **overrides})


def a_team(session_id: str, **overrides: Any) -> Team:
    members = (
        a_member(session_id, "agt_a", **overrides),
        a_member(session_id, "agt_b", state=State.failed, detail="stopped", **overrides),
    )
    return Team(session_id=session_id, group="reviewers", members=members)


class Harness:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.woken = 0
        self.waker = Waker(store, EventEmitter(SqlEventLog(store), Snapshot()), wake=self.wake)

    def wake(self) -> None:
        self.woken += 1

    async def turns(self, session_id: str) -> list[dict[str, Any]]:
        return await self.store.records(ACCOUNT, session_id, "turns")

    async def events(self, session_id: str) -> list[dict[str, Any]]:
        return await self.store.records(ACCOUNT, session_id, "events")


async def test_a_member_of_a_group_is_announced_and_wakes_nothing(store: SessionStore) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_finished(a_member(session, "agt_a"))

    [finished] = [row for row in await harness.events(session) if row["type"] == WORK_FINISHED]
    assert finished["data"]["group"] == "reviewers"
    assert await harness.turns(session) == []
    assert harness.woken == 0


async def test_a_group_ending_opens_one_turn_with_one_line_naming_every_member(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    team = a_team(session)

    await harness.waker.on_team_finished(team)

    [turn] = await harness.turns(session)
    assert turn["input"] == {
        "events": [{"type": WAKE_INPUT, "group": "reviewers", "work_ids": ["agt_a", "agt_b"]}]
    }
    [item] = await store.records(ACCOUNT, session, "items")
    assert item["type"] == NOTICE_KIND
    assert item["content"] == team_wake_line(team)
    assert str(item["content"]).startswith(
        "[harness: group reviewers (2 members) has ended: reviewer agt_a - succeeded - "
        "about 300 tokens of result; reviewer agt_b - failed - stopped. "
        "Nothing here is from the person."
    )
    events = await harness.events(session)
    group = next(row for row in events if row["type"] == WORK_GROUP_FINISHED)
    assert group["data"] == {
        "group": "reviewers",
        "members": [
            {"work_id": "agt_a", "role": "reviewer", "state": "succeeded", "result_tokens": 300},
            {"work_id": "agt_b", "role": "reviewer", "state": "failed", "result_tokens": 300},
        ],
        "wake": True,
    }
    woke = next(row for row in events if row["type"] == WORK_WOKE)
    assert woke["data"] == {"group": "reviewers", "work_ids": ["agt_a", "agt_b"]}
    assert harness.woken == 1


async def test_a_group_that_asked_for_no_wake_is_only_announced(store: SessionStore) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_team_finished(a_team(session, wake=False))

    assert WORK_GROUP_FINISHED in [row["type"] for row in await harness.events(session)]
    assert await harness.turns(session) == []


async def test_a_group_with_no_account_is_announced_without_asking_the_store(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)

    await harness.waker.on_team_finished(a_team(session, account_id="", wake=False))

    assert WORK_GROUP_FINISHED in [row["type"] for row in await harness.events(session)]


async def test_a_group_of_a_session_that_is_gone_wakes_nothing(store: SessionStore) -> None:
    harness = Harness(store)
    session = await a_session(store)
    await store.delete(ACCOUNT, session)

    await harness.waker.on_team_finished(a_team(session))

    assert harness.woken == 0


async def test_a_group_ending_during_a_turn_is_held_and_dropped_once_all_were_read(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await a_session(store)
    live = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": "go"}], "k1"
    )
    team = a_team(session)
    await harness.waker.on_team_finished(team)
    assert len(await harness.turns(session)) == 1, "held: the running turn will see it"

    team.members[0].fetched = True
    await store.finish_turn(ACCOUNT, str(live["id"]), "completed")
    await harness.waker.flush(session)
    assert [turn["status"] for turn in await harness.turns(session)] == ["completed", "queued"]

    again = a_team(session)
    for member in again.members:
        member.fetched = True
    await harness.waker.on_team_finished(again)
    assert len(await harness.turns(session)) == 2, "held behind the queued wake"
    [queued] = [turn for turn in await harness.turns(session) if turn["status"] == "queued"]
    await store.finish_turn(ACCOUNT, str(queued["id"]), "completed")
    await harness.waker.flush(session)
    assert len(await harness.turns(session)) == 2, "every result was read: no news left"
    assert harness.woken == 1
