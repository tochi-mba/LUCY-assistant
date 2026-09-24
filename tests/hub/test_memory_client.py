"""The memory client projects rows and never forwards an account id."""

from __future__ import annotations

from lucy_api.clients.memory import (
    Draft,
    HttpMemoryClient,
    TopicCard,
    _note,
    _scope,
    _topic,
    as_dict,
)
from lucy_api.clients.testing import Answer, FakeHttp, ReadRecorder


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


RECORDED_TOPIC = {
    "id": "top_41acd2de8f0fb53452eafdc8874f14d0",
    "account_id": "acct_57d7842b361d4a4da10510a5599a3268",
    "profile": "personal",
    "key": "drink preference",
    "title": "Drink preference",
    "summary": "Prefers tea over coffee",
    "kind": "fact",
    "memory_count": 1,
    "unconfirmed": 0,
    "importance": 5,
    "first_seen": 1790190222.509364,
    "last_seen": 1790253148.4500523,
    "last_summarised_at": None,
    "revision": 1,
}
"""One row of `GET /v1/internal/memory/topics`, recorded from a running Memory-api on
2026-09-24 and copied here verbatim.

Recorded rather than written, because a hand-written row is written by whoever wrote the
parser, and the two agree with each other instead of with the service. That is how this
client came to read `count` and `unread` -- names its own test sent and the service never
has -- and why every prompt it fed said "0 memories". Re-record it from the running family
if Memory-api's `Topic` model changes; do not edit it to match the parser.
"""

OPTIONAL_TOPIC_KEYS = frozenset({"trust"})
"""Keys the parser may ask for that the service is not expected to send.

`trust`: Memory-api vouches server-side instead (a topic made only of unvouched members
never reaches the index), so an absent `trust` is correct and reads as `stated`. The parser
still asks, so a topic that ever does arrive marked otherwise is held back.
"""


def test_a_recorded_topic_row_decodes_to_the_counts_the_service_meant() -> None:
    """The bug, named: one vouched memory, not zero."""
    card = _topic(RECORDED_TOPIC)
    assert card.count == 1
    assert card.unconfirmed == 0
    assert card.importance == 5.0
    assert card.trust == "stated"
    assert card.last_seen is not None
    assert card.last_seen.year == 2026


def test_the_topic_parser_reads_nothing_the_service_does_not_send() -> None:
    """The general guard. Any name asked for and absent from a recorded row is a field this
    client reads as a default on every call, which is the defect class, not a style point."""
    row = ReadRecorder(RECORDED_TOPIC)
    _topic(row)
    assert row.absent <= OPTIONAL_TOPIC_KEYS, row.absent - OPTIONAL_TOPIC_KEYS


def test_unconfirmed_members_are_carried_not_folded_into_the_count() -> None:
    """They came from a page or a tool, never reach retrieval, and the service keeps them out
    of `memory_count` on purpose. Adding them in would claim knowledge the hub cannot use."""
    card = _topic({**RECORDED_TOPIC, "memory_count": 2, "unconfirmed": 3})
    assert card.count == 2
    assert card.unconfirmed == 3


def test_a_topic_the_service_marks_untrusted_is_still_read_as_untrusted() -> None:
    """Defence in depth: the service filters today, and the hub still holds back a topic
    that ever arrives marked otherwise rather than trusting the default."""
    assert _topic({**RECORDED_TOPIC, "trust": "untrusted"}).trust == "untrusted"


async def test_the_topic_index_and_its_expansion_never_carry_an_account_id() -> None:
    http = FakeHttp(
        Answer(
            body={
                "data": [
                    {**RECORDED_TOPIC, "id": "top_1", "title": "Tea", "account_id": "secret"},
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
    assert topics[0].count == 1
    assert topics[0].importance == 5.0
    assert topics[1].importance == 0.0
    assert topics[2].importance == 0.0
    assert "account_id" not in TopicCard.__dataclass_fields__
    assert from_memories[0].body == "prefers tea"
    assert from_data[0].id == "mem_2"
    assert all("/v1/internal/memory/topics" in call.url for call in http.calls)
    assert "account_id" not in as_dict(from_memories[0])
