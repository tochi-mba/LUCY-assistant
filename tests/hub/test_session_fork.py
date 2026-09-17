"""A fork is a copy of what was said, not a second writer on the same directory.

Two claims are being checked, and they are the two a byte copy would get wrong.

**Ids are remapped.** Every copied item has a new id and a parent that points at another
copy, never back into the session it came from. The test walks the fork's chain and asserts
that no id anywhere in it appears in the original -- because the way this fails in practice
is not an exception, it is a fork that reads correctly today and strands itself the moment
the parent session is deleted.

**The workspace is detached.** Branching a conversation must not share file access.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api import __version__
from lucy_api.core.errors import LucyError
from lucy_api.sessions.fork import fork_session
from lucy_api.sessions.items import append_item
from lucy_api.sessions.models import CreateSession, Outcome
from lucy_api.sessions.sql_store import NewItem, identifier
from lucy_api.sessions.turns import close_turn, open_turn

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore

OWNER = "acct_owner"
STRANGER = "acct_stranger"
ENVIRONMENT = "env_shared"
DIRECTORY = "projects/lucy"


async def a_session(store: SessionStore, account: str = OWNER, **fields: object) -> str:
    created = await store.create(account, CreateSession(**fields), identifier("key"))
    return str(created["id"])


def said(text: str, role: str = "user") -> NewItem:
    return NewItem(kind="message", role=role, content={"text": text}, tokens=len(text))


async def a_conversation(store: SessionStore, session: str, lines: int = 3) -> list[Any]:
    return [
        await append_item(store, OWNER, session, said(f"line {index}")) for index in range(lines)
    ]


async def give_a_workspace(store: SessionStore, session: str) -> None:
    def apply(db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE sessions SET workspace_environment_id=?,workspace_rel=? WHERE id=?",
            (ENVIRONMENT, DIRECTORY, session),
        )

    await store.transaction(apply)


async def test_a_fork_copies_every_item_and_gives_each_one_a_new_identity(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    original = await a_conversation(sessions_store, session)

    fork = await fork_session(sessions_store, OWNER, session)

    copies = await sessions_store.records(OWNER, str(fork["id"]), "items")
    assert [row["content"] for row in copies] == [row["content"] for row in original]
    assert [row["seq"] for row in copies] == [1, 2, 3]
    assert {row["id"] for row in copies}.isdisjoint({row["id"] for row in original})


async def test_a_forks_parent_chain_points_only_at_the_fork(
    sessions_store: SessionStore,
) -> None:
    # The whole reason ids are remapped: a chain that pointed back would strand the copy the
    # day the original is deleted, and nothing would notice until then.
    session = await a_session(sessions_store)
    original = await a_conversation(sessions_store, session)

    fork = await fork_session(sessions_store, OWNER, session)

    copies = await sessions_store.records(OWNER, str(fork["id"]), "items")
    inside = {row["id"] for row in copies}
    parents = [row["parent_id"] for row in copies]
    assert parents[0] is None
    assert parents[1:] == [row["id"] for row in copies[:-1]]
    assert all(parent in inside for parent in parents[1:])
    assert inside.isdisjoint({row["id"] for row in original})


async def test_deleting_the_session_a_fork_came_from_leaves_the_fork_readable(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await a_conversation(sessions_store, session)
    fork = await fork_session(sessions_store, OWNER, session)

    await sessions_store.delete(OWNER, session)

    copies = await sessions_store.records(OWNER, str(fork["id"]), "items")
    assert len(copies) == 3


async def test_a_fork_taken_at_an_item_stops_there(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    original = await a_conversation(sessions_store, session)

    fork = await fork_session(sessions_store, OWNER, session, str(original[1]["id"]))

    copies = await sessions_store.records(OWNER, str(fork["id"]), "items")
    assert [row["content"] for row in copies] == [row["content"] for row in original[:2]]
    assert fork["forked_from_item"] == original[1]["id"]


async def test_a_fork_taken_from_the_end_still_records_where_the_two_diverged(
    sessions_store: SessionStore,
) -> None:
    # The parent keeps growing after the fork, so "all of it" has to be pinned to an item or
    # it stops meaning anything an hour later.
    session = await a_session(sessions_store)
    original = await a_conversation(sessions_store, session)

    fork = await fork_session(sessions_store, OWNER, session)

    assert fork["forked_from_item"] == original[-1]["id"]
    assert fork["parent_session_id"] == session


async def test_forking_a_session_nothing_has_been_said_in_yet_is_allowed(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    fork = await fork_session(sessions_store, OWNER, session)

    assert fork["forked_from_item"] is None
    assert await sessions_store.records(OWNER, str(fork["id"]), "items") == []


async def test_an_item_that_is_not_in_this_session_is_not_a_place_to_fork_from(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await a_conversation(sessions_store, session)
    elsewhere = await a_session(sessions_store)
    foreign = await append_item(sessions_store, OWNER, elsewhere, said("over there"))

    with pytest.raises(LucyError) as caught:
        await fork_session(sessions_store, OWNER, session, str(foreign["id"]))

    assert caught.value.code == "not-found"
    # Nothing was created on the way to refusing.
    assert len((await sessions_store.list_sessions(OWNER, 100, None, None, "asc"))["data"]) == 2


async def test_forking_another_accounts_session_finds_nothing(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await a_conversation(sessions_store, session)

    with pytest.raises(LucyError) as caught:
        await fork_session(sessions_store, STRANGER, session)

    assert caught.value.code == "not-found"


async def test_a_fork_detaches_its_parents_workspace(
    sessions_store: SessionStore,
) -> None:
    # Deliberate, and the dangerous half of forking: an agent in the fork editing a file
    # changes the file the original session sees.
    session = await a_session(sessions_store)
    await give_a_workspace(sessions_store, session)
    await a_conversation(sessions_store, session, lines=1)

    fork = await fork_session(sessions_store, OWNER, session)

    assert fork["workspace_environment_id"] is None
    assert fork["workspace_rel"] is None
    forked = [
        event
        for event in await sessions_store.records(OWNER, str(fork["id"]), "events")
        if event["type"] == "lucy.session.forked"
    ]
    assert forked[0]["data"]["workspace_shared"] is False
    parent = await sessions_store.get(OWNER, session)
    assert parent["workspace_environment_id"] == ENVIRONMENT
    assert parent["workspace_rel"] == DIRECTORY


async def test_a_fork_of_a_session_with_no_workspace_says_there_is_nothing_shared(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    fork = await fork_session(sessions_store, OWNER, session)

    forked = [
        event
        for event in await sessions_store.records(OWNER, str(fork["id"]), "events")
        if event["type"] == "lucy.session.forked"
    ]
    assert forked[0]["data"]["workspace_shared"] is False
    assert fork["workspace_environment_id"] is None


async def test_the_session_that_was_branched_hears_about_it_too(
    sessions_store: SessionStore,
) -> None:
    # A client watching the original has no other way to learn that a second conversation
    # now edits the same files.
    session = await a_session(sessions_store)
    await a_conversation(sessions_store, session, lines=1)

    fork = await fork_session(sessions_store, OWNER, session)

    announced = [
        event
        for event in await sessions_store.records(OWNER, session, "events")
        if event["type"] == "lucy.session.forked"
    ]
    assert announced[0]["data"]["session_id"] == fork["id"]


async def test_a_fork_does_not_replay_the_conversation_on_the_event_stream(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await a_conversation(sessions_store, session, lines=5)

    fork = await fork_session(sessions_store, OWNER, session)

    kinds = [
        event["type"] for event in await sessions_store.records(OWNER, str(fork["id"]), "events")
    ]
    assert kinds == ["lucy.session.created", "lucy.session.forked"]


async def test_a_fork_keeps_the_settings_and_starts_with_nothing_spent(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(
        sessions_store,
        title="Planning",
        model="openai:gpt-5",
        permission_mode="plan",
        input_policy="reject",
        incognito=True,
    )
    await a_conversation(sessions_store, session, lines=1)

    fork = await fork_session(sessions_store, OWNER, session)

    assert fork["title"] == "Planning"
    assert fork["model"] == "openai:gpt-5"
    assert fork["permission_mode"] == "plan"
    assert fork["input_policy"] == "reject"
    assert fork["incognito"] == 1
    assert fork["harness_version"] == __version__
    assert (fork["input_tokens"], fork["output_tokens"], fork["cost_micros"]) == (0, 0, 0)


async def test_a_fork_of_a_busy_session_starts_idle_and_inherits_no_turns(
    sessions_store: SessionStore,
) -> None:
    # A turn belongs to the run that produced it. Copying one would double-count what it
    # spent, and leaving its id on a copied item would point into another session's ledger.
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, {"events": []})
    await close_turn(sessions_store, OWNER, str(turn["id"]), Outcome("running"))
    await sessions_store.append(
        OWNER, session, NewItem("message", "user", "mid-turn", turn=str(turn["id"]))
    )

    fork = await fork_session(sessions_store, OWNER, session)

    assert fork["status"] == "idle"
    assert await sessions_store.records(OWNER, str(fork["id"]), "turns") == []
    copies = await sessions_store.records(OWNER, str(fork["id"]), "items")
    assert [row["turn_id"] for row in copies] == [None]


async def test_an_archived_session_forks_into_a_live_one(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    await a_conversation(sessions_store, session, lines=1)
    await sessions_store.update(OWNER, session, {"archived": True})

    fork = await fork_session(sessions_store, OWNER, session)

    assert fork["archived_at"] is None


async def test_a_fork_can_itself_be_forked(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    original = await a_conversation(sessions_store, session)
    first = await fork_session(sessions_store, OWNER, session, str(original[1]["id"]))

    second = await fork_session(sessions_store, OWNER, str(first["id"]))

    assert second["parent_session_id"] == first["id"]
    copies = await sessions_store.records(OWNER, str(second["id"]), "items")
    assert [row["content"] for row in copies] == [row["content"] for row in original[:2]]
    assert {row["id"] for row in copies}.isdisjoint(
        {row["id"] for row in await sessions_store.records(OWNER, str(first["id"]), "items")}
    )
