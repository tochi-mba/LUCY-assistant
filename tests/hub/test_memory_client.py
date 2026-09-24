"""The memory client projects rows and never forwards an account id."""

from __future__ import annotations

from lucy_api.clients.memory import (
    CORRECTION_CARRIES,
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


# --- one memory, as the service returns it ----------------------------------------------------

RECORDED_MEMORY = {
    "title": "Drink preference",
    "body": "Prefers tea over coffee.",
    "value": None,
    "kind": "fact",
    "scope": "profile",
    "profile": "personal",
    "session_id": None,
    "source": "conversation",
    "trust": "stated",
    "confidence": 1.0,
    "importance": 5,
    "occurred_at": None,
    "valid_from": 1790190222.50511,
    "expires_at": None,
    "id": "mem_a473e59d4d06bc51849c06eb0042bbf0",
    "account_id": "acct_57d7842b361d4a4da10510a5599a3268",
    "asserted_by": "lucy-api",
    "topic_id": "top_41acd2de8f0fb53452eafdc8874f14d0",
    "created_at": 1790190222.50511,
    "updated_at": 1790190222.50511,
    "last_accessed_at": 1790254787.7675898,
    "access_count": 6,
    "confirmed_at": None,
    "valid_to": None,
    "supersedes_id": None,
    "superseded_by_id": None,
    "forgotten_at": None,
    "revision": 1,
}
"""`GET /v1/internal/memory/{id}`, recorded from a running Memory-api on 2026-09-24.

A fact Lucy wrote itself, so profile-scoped -- which is every note the hub writes, and is
why a correction sent as `{title, body}` was refused for all of them. Re-record rather than
edit if Memory-api's `Memory` model changes.
"""

MEMORY_ONLY = frozenset(
    {
        "id",
        "account_id",
        "asserted_by",
        "topic_id",
        "created_at",
        "updated_at",
        "last_accessed_at",
        "access_count",
        "confirmed_at",
        "valid_to",
        "supersedes_id",
        "superseded_by_id",
        "forgotten_at",
        "revision",
    }
)
"""Fields `Memory` adds to `MemoryInput` (Memory-api `domain/models.py`). The correct route
takes a `MemoryInput` with `extra="forbid"`, so sending any of these back is a 422."""


def _corrected(http: FakeHttp) -> dict[str, object]:
    posted = http.calls[-1]
    assert posted.method == "POST"
    assert isinstance(posted.json, dict)
    return posted.json


async def test_a_correction_keeps_the_scope_it_would_otherwise_be_refused_for() -> None:
    """The bug, named: a profile-scoped fact, corrected, stays profile-scoped -- which is the
    one thing the store checks before it will accept a correction at all."""
    http = FakeHttp(
        Answer(body=RECORDED_MEMORY),
        Answer(body={**RECORDED_MEMORY, "id": "mem_new", "body": "Drinks green tea."}),
    )
    client = HttpMemoryClient(http, "http://memory.test")

    note = await client.correct(
        str(RECORDED_MEMORY["id"]), "Drink preference", "Drinks green tea.", profile="personal"
    )

    assert http.calls[0].method == "GET"
    assert http.calls[0].url.endswith(f"/v1/internal/memory/{RECORDED_MEMORY['id']}")
    assert http.calls[1].url.endswith(f"/v1/internal/memory/{RECORDED_MEMORY['id']}/correct")
    body = _corrected(http)
    assert (body["scope"], body["profile"], body["session_id"]) == ("profile", "personal", None)
    assert body["title"] == "Drink preference"
    assert body["body"] == "Drinks green tea."
    assert note.id == "mem_new"


async def test_a_correction_keeps_the_provenance_the_defaults_would_overwrite() -> None:
    http = FakeHttp(Answer(body=RECORDED_MEMORY), Answer(body=RECORDED_MEMORY))
    await HttpMemoryClient(http, "http://memory.test").correct("mem_1", "t", "b")
    body = _corrected(http)
    assert body["kind"] == "fact"
    assert body["source"] == "conversation"
    assert body["confidence"] == 1.0
    assert body["importance"] == 5


async def test_correcting_an_untrusted_note_does_not_launder_its_trust() -> None:
    """Left to its defaults, a correction arrives `stated`. An untrusted note from a page,
    edited, would then enter retrieval -- trust acquired by an edit nobody vouched for."""
    untrusted = {**RECORDED_MEMORY, "trust": "untrusted", "source": "https://example.invalid"}
    http = FakeHttp(Answer(body=untrusted), Answer(body=untrusted))
    await HttpMemoryClient(http, "http://memory.test").correct("mem_1", "t", "b")
    assert _corrected(http)["trust"] == "untrusted"
    assert _corrected(http)["source"] == "https://example.invalid"


async def test_a_correction_body_is_one_the_correct_route_would_accept() -> None:
    """Carry the words and the provenance, and nothing only a stored memory has: the route's
    model forbids extra fields. `valid_from` stays behind so the store can stamp it."""
    http = FakeHttp(Answer(body=RECORDED_MEMORY), Answer(body=RECORDED_MEMORY))
    await HttpMemoryClient(http, "http://memory.test").correct("mem_1", "t", "b")
    body = _corrected(http)
    assert not set(body) & MEMORY_ONLY
    assert "valid_from" not in body
    assert set(body) == {*CORRECTION_CARRIES, "title", "body"}


async def test_an_original_that_is_not_a_document_still_sends_the_new_words() -> None:
    http = FakeHttp(Answer(body="not an object"), Answer(body=RECORDED_MEMORY))
    await HttpMemoryClient(http, "http://memory.test").correct("mem_1", "t", "b")
    assert _corrected(http) == {"title": "t", "body": "b"}


async def test_an_original_missing_some_fields_carries_only_what_it_has() -> None:
    sparse = {"scope": "session", "profile": "personal", "session_id": "ses_1"}
    http = FakeHttp(Answer(body=sparse), Answer(body=RECORDED_MEMORY))
    await HttpMemoryClient(http, "http://memory.test").correct("mem_1", "t", "b")
    assert _corrected(http) == {**sparse, "title": "t", "body": "b"}


def test_the_note_parser_reads_nothing_the_service_does_not_send() -> None:
    row = ReadRecorder(RECORDED_MEMORY)
    _note(row)
    assert not row.absent, row.absent
