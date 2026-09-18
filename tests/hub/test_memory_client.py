"""The memory client projects rows and never forwards an account id."""

from __future__ import annotations

from lucy_api.clients.memory import Draft, HttpMemoryClient, _note, _scope, as_dict
from lucy_api.clients.testing import Answer, FakeHttp


def test_a_row_that_is_not_an_object_becomes_an_empty_note() -> None:
    assert _note("nope").id == ""


def test_a_fact_without_a_profile_is_an_account_memory() -> None:
    assert _scope(kind="fact", profile="", session_id="ses") == {"scope": "account"}


def test_an_episode_is_bound_to_the_session() -> None:
    assert _scope(kind="episode", profile="work", session_id="ses_1")["scope"] == "session"


def test_as_dict_keeps_the_fields_a_tool_result_may_carry() -> None:
    note = _note({"id": "mem_1", "title": "tea", "body": "yes", "confirmed_at": 1})
    assert as_dict(note)["confirmed"] is True
    assert set(as_dict(note)) == {
        "id",
        "title",
        "body",
        "kind",
        "trust",
        "source",
        "confirmed",
    }


async def test_listing_and_mutations_use_the_memory_audience() -> None:
    stored = {
        "id": "mem_1",
        "title": "tea",
        "body": "prefers tea",
        "kind": "fact",
        "trust": "stated",
        "source": "you",
        "account_id": "secret",
    }
    http = FakeHttp(
        Answer(body={"data": [stored]}),
        Answer(body={"data": [stored]}),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body={"data": [{"label": "human", "body": "a person", "char_limit": 4000}]}),
    )
    client = HttpMemoryClient(http, "http://memory.test")

    listed = await client.listing(profile="personal")
    searched = await client.search("tea", profile="personal")
    remembered = await client.remember(
        Draft(title="tea", body="prefers tea", kind="fact", profile="personal", session_id="ses")
    )
    confirmed = await client.confirm("mem_1", profile="personal")
    corrected = await client.correct("mem_1", "tea", "green tea", profile="personal")
    forgotten = await client.forget("mem_1", profile="personal")
    blocks = await client.blocks(profile="personal")

    assert listed[0].title == "tea"
    assert searched[0].title == "tea"
    assert remembered.id == "mem_1"
    assert confirmed.id == "mem_1"
    assert corrected.id == "mem_1"
    assert forgotten.id == "mem_1"
    assert blocks[0].label == "human"
    assert all(call.audience == "memory-api" for call in http.calls)
    assert all("/v1/internal/memory" in call.url for call in http.calls)
    assert http.calls[1].url.endswith("/v1/internal/memory/search")
    assert "account_id" not in as_dict(listed[0])


async def test_the_topic_index_and_its_expansion_never_carry_an_account_id() -> None:
    http = FakeHttp(
        Answer(
            body={
                "data": [
                    {
                        "id": "top_1",
                        "key": "tea",
                        "title": "Tea",
                        "summary": "How they take it",
                        "count": 3,
                        "importance": "0.5",
                        "unread": 1,
                        "trust": "stated",
                        "last_seen": "2026-09-01T12:00:00Z",
                        "account_id": "secret",
                    },
                    {"id": "top_bad", "importance": "not-a-number"},
                    {"id": "top_obj", "importance": {"nested": True}},
                    "not-an-object",
                ]
            }
        ),
        Answer(
            body={
                "memories": [
                    {
                        "id": "mem_1",
                        "title": "tea",
                        "body": "prefers tea",
                        "account_id": "secret",
                    }
                ]
            }
        ),
        Answer(body={"data": [{"id": "mem_2", "title": "also", "body": "green"}]}),
    )
    client = HttpMemoryClient(http, "http://memory.test")

    topics = await client.topics(profile="personal")
    from_memories = await client.topic_memories("top_1", profile="personal")
    from_data = await client.topic_memories("top_1", profile="personal")

    assert [card.id for card in topics] == ["top_1", "top_bad", "top_obj"]
    assert topics[0].title == "Tea"
    assert topics[0].importance == 0.5
    assert topics[1].importance == 0.0
    assert topics[2].importance == 0.0
    assert topics[0].unread == 1
    assert from_memories[0].body == "prefers tea"
    assert from_data[0].id == "mem_2"
    assert all("/v1/internal/memory/topics" in call.url for call in http.calls)
    assert "account_id" not in as_dict(from_memories[0])
