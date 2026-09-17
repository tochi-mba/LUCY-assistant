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
        Answer(body=stored),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body={"data": [{"label": "human", "body": "a person", "char_limit": 4000}]}),
    )
    client = HttpMemoryClient(http, "http://memory.test")

    listed = await client.listing(profile="personal")
    remembered = await client.remember(
        Draft(title="tea", body="prefers tea", kind="fact", profile="personal", session_id="ses")
    )
    confirmed = await client.confirm("mem_1", profile="personal")
    corrected = await client.correct("mem_1", "tea", "green tea", profile="personal")
    forgotten = await client.forget("mem_1", profile="personal")
    blocks = await client.blocks(profile="personal")

    assert listed[0].title == "tea"
    assert remembered.id == "mem_1"
    assert confirmed.id == "mem_1"
    assert corrected.id == "mem_1"
    assert forgotten.id == "mem_1"
    assert blocks[0].label == "human"
    assert all(call.audience == "memory-api" for call in http.calls)
    assert "account_id" not in as_dict(listed[0])
