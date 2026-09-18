"""A transcript is append-only and its parent chain is allowed to fork.

Two properties are being defended here and they pull in opposite directions, which is why
both are tested at once. ``seq`` must only ever climb, because every cursor over the log is
built on it; ``parent_id`` must be free to point sideways, because editing a message and
regenerating is a second answer to the same question rather than a reply to the first one.

The awkward case is the first item in a session. Its parent is nothing at all, and
regenerating it has to produce another item whose parent is nothing at all -- not one
chained to whatever happened to be written last. That is the difference between an argument
that was omitted and one that was passed as ``None``, and it is the reason the default is a
sentinel rather than ``None``.

A parent from another session is refused rather than stored. Everything that reads a
transcript reads one session at a time, so a chain that leaves the session is a chain
nothing can walk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.sessions.items import append_item, list_agent_items, list_items, regenerate_item
from lucy_api.sessions.models import CreateSession, Cursor
from lucy_api.sessions.sql_store import NewItem, identifier

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore

OWNER = "acct_owner"
STRANGER = "acct_stranger"


async def a_session(store: SessionStore, account: str = OWNER) -> str:
    created = await store.create(account, CreateSession(), identifier("key"))
    return str(created["id"])


def said(text: str, role: str = "user") -> NewItem:
    return NewItem(kind="message", role=role, content={"text": text})


async def transcript(store: SessionStore, session: str, account: str = OWNER) -> list[Any]:
    return await store.records(account, session, "items")


async def test_an_item_appended_without_a_parent_follows_whatever_came_before(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    first = await append_item(sessions_store, OWNER, session, said("hello"))
    second = await append_item(sessions_store, OWNER, session, said("and again"))

    assert first["parent_id"] is None
    assert first["seq"] == 1
    assert second["parent_id"] == first["id"]
    assert second["seq"] == 2


async def test_an_item_can_name_an_earlier_item_as_its_parent(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    first = await append_item(sessions_store, OWNER, session, said("hello"))
    await append_item(sessions_store, OWNER, session, said("an answer", role="assistant"))

    branched = await append_item(
        sessions_store, OWNER, session, said("hello, rephrased"), parent=first["id"]
    )

    # It is a sibling of the answer, not a reply to it, and it still sits at the end.
    assert branched["parent_id"] == first["id"]
    assert branched["seq"] == 3


async def test_an_item_can_be_written_with_no_parent_at_all_on_purpose(
    sessions_store: SessionStore,
) -> None:
    # Passing None is not the same as omitting the argument: it says this item answers
    # nothing, which is what a regenerated opening message is.
    session = await a_session(sessions_store)
    await append_item(sessions_store, OWNER, session, said("hello"))

    rooted = await append_item(sessions_store, OWNER, session, said("start over"), parent=None)

    assert rooted["parent_id"] is None
    assert rooted["seq"] == 2


async def test_a_parent_from_another_session_is_refused_rather_than_stored(
    sessions_store: SessionStore,
) -> None:
    here = await a_session(sessions_store)
    elsewhere = await a_session(sessions_store)
    foreign = await append_item(sessions_store, OWNER, elsewhere, said("over there"))

    with pytest.raises(LucyError) as caught:
        await append_item(sessions_store, OWNER, here, said("hello"), parent=foreign["id"])

    assert caught.value.code == "unknown-parent"
    assert caught.value.status == 404
    # The message names the session, never the id it was handed back.
    assert foreign["id"] not in str(caught.value)
    assert await transcript(sessions_store, here) == []


async def test_a_parent_that_does_not_exist_is_refused_the_same_way(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    with pytest.raises(LucyError) as caught:
        await append_item(sessions_store, OWNER, session, said("hello"), parent="itm_invented")

    assert caught.value.code == "unknown-parent"


async def test_appending_with_a_parent_still_refuses_another_accounts_session(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    first = await append_item(sessions_store, OWNER, session, said("hello"))

    with pytest.raises(LucyError) as caught:
        await append_item(sessions_store, STRANGER, session, said("mine now"), parent=first["id"])

    assert caught.value.code == "not-found"
    assert len(await transcript(sessions_store, session)) == 1


async def test_regenerating_an_answer_makes_a_sibling_and_leaves_the_original_alone(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    question = await append_item(sessions_store, OWNER, session, said("what is the weather"))
    answer = await append_item(sessions_store, OWNER, session, said("cold", role="assistant"))

    again = await regenerate_item(
        sessions_store, OWNER, str(answer["id"]), said("cold and wet", role="assistant")
    )

    assert again["parent_id"] == question["id"] == answer["parent_id"]
    assert again["id"] != answer["id"]
    assert [row["id"] for row in await transcript(sessions_store, session)] == [
        question["id"],
        answer["id"],
        again["id"],
    ]


async def test_regenerating_the_first_message_produces_another_item_with_no_parent(
    sessions_store: SessionStore,
) -> None:
    # The case a single None default would get wrong: this must not become a reply to the
    # conversation it is trying to restart.
    session = await a_session(sessions_store)
    first = await append_item(sessions_store, OWNER, session, said("hello"))
    await append_item(sessions_store, OWNER, session, said("hi", role="assistant"))

    again = await regenerate_item(sessions_store, OWNER, str(first["id"]), said("hello again"))

    assert again["parent_id"] is None
    assert again["seq"] == 3


async def test_regenerating_an_item_belonging_to_somebody_else_finds_nothing(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    item = await append_item(sessions_store, OWNER, session, said("hello"))

    with pytest.raises(LucyError) as caught:
        await regenerate_item(sessions_store, STRANGER, str(item["id"]), said("mine now"))

    assert caught.value.code == "not-found"


async def test_a_page_of_a_transcript_reads_forwards_and_resumes_from_a_cursor(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    written = [
        await append_item(sessions_store, OWNER, session, said(f"line {index}"))
        for index in range(5)
    ]

    first = await list_items(sessions_store, OWNER, session, Cursor(limit=2))
    second = await list_items(
        sessions_store, OWNER, session, Cursor(limit=2, after=str(first["last_id"]))
    )

    assert [row["id"] for row in first["data"]] == [row["id"] for row in written[:2]]
    assert first["has_more"] is True
    assert [row["id"] for row in second["data"]] == [row["id"] for row in written[2:4]]


async def test_a_transcript_can_be_read_newest_first(sessions_store: SessionStore) -> None:
    session = await a_session(sessions_store)
    first = await append_item(sessions_store, OWNER, session, said("oldest"))
    last = await append_item(sessions_store, OWNER, session, said("newest"))

    page = await list_items(sessions_store, OWNER, session, Cursor(order="desc"))

    assert [row["id"] for row in page["data"]] == [last["id"], first["id"]]
    assert page["has_more"] is False


async def test_helper_items_are_absent_from_the_parent_page(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    parent = await append_item(sessions_store, OWNER, session, said("hello"))
    helper = await sessions_store.append(
        OWNER,
        session,
        NewItem("message", "assistant", {"text": "child"}, agent_id="agt_reviewer"),
    )

    listed = await list_items(sessions_store, OWNER, session, Cursor())
    child = await list_agent_items(sessions_store, OWNER, session, "agt_reviewer", Cursor())
    nobody = await list_agent_items(sessions_store, OWNER, session, "agt_missing", Cursor())

    assert [row["id"] for row in listed["data"]] == [parent["id"]]
    assert [row["id"] for row in child["data"]] == [helper["id"]]
    assert nobody["data"] == []


async def test_a_transcript_belonging_to_somebody_else_is_not_readable(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await append_item(sessions_store, OWNER, session, said("private"))

    with pytest.raises(LucyError) as caught:
        await list_items(sessions_store, STRANGER, session, Cursor())

    assert caught.value.code == "not-found"
