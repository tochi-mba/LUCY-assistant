"""The shared half of every client: one call, one translation, one forgiving decode.

The decoding tests look like trivia and are not. Nine services are released separately, so
a field that stops arriving is a normal Tuesday, and the question each of these pins is
whether that costs one field or the whole turn. The answer has to be the same in every
client, which is why the readers live in one module and are tested once.

The `Sibling` tests are about the request rather than the response: the audience a token was
minted for and the profile header are both invisible in a successful answer, and both are
the difference between reading this person's music and somebody else's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from lucy_api.clients.errors import AbsentError
from lucy_api.clients.testing import Answer, ExhaustedError, FakeHttp, problem
from lucy_api.clients.transport import (
    PROFILE_HEADER,
    Sibling,
    capped,
    field,
    flag,
    given,
    moment,
    nested,
    number,
    rows,
    segment,
    text,
    trust_from,
)
from lucy_api.context.types import Trust


def sibling(*answers: Answer) -> tuple[Sibling, FakeHttp]:
    """A sibling wired to a scripted transport, which is every test in this file."""
    http = FakeHttp(*answers)
    return Sibling(
        http=http, base_url="http://music.test/", service="music", audience="music-api"
    ), http


async def test_a_call_carries_the_audience_its_token_must_be_minted_for() -> None:
    api, http = sibling(Answer(body={"ok": True}))

    await api.send("GET", "/v1/player")

    assert http.last.audience == "music-api"
    assert http.last.url == "http://music.test/v1/player"
    assert http.last.method == "GET"


async def test_a_profile_travels_as_a_header_and_is_absent_when_nobody_named_one() -> None:
    api, http = sibling(Answer(), Answer())

    await api.send("GET", "/v1/player", profile="work")
    await api.send("GET", "/v1/player")

    assert http.calls[0].headers == {PROFILE_HEADER: "work"}
    assert http.calls[1].headers is None


async def test_a_body_and_query_reach_the_call_exactly_as_they_were_handed_over() -> None:
    api, http = sibling(Answer(body={"job_id": "j-1"}))

    body = await api.send("POST", "/v1/downloads", body={"items": []}, params={"limit": 5})

    assert body == {"job_id": "j-1"}
    assert http.last.json == {"items": []}
    assert http.last.params == {"limit": 5}


async def test_a_no_content_answer_decodes_to_nothing_rather_than_to_a_decode_error() -> None:
    api, _ = sibling(Answer(status_code=204))

    assert await api.send("DELETE", "/v1/connections/music") is None


async def test_a_refusal_is_translated_before_the_caller_ever_sees_a_status() -> None:
    api, _ = sibling(problem(404, detail="no such profile"))

    with pytest.raises(AbsentError):
        await api.send("GET", "/v1/profiles/personal")


async def test_a_call_nobody_scripted_fails_the_test_rather_than_answering_emptily() -> None:
    api, _ = sibling()

    with pytest.raises(ExhaustedError):
        await api.send("GET", "/v1/player")


def test_asking_a_transport_that_was_never_called_what_it_was_called_with_is_an_error() -> None:
    with pytest.raises(LookupError):
        _ = FakeHttp().last


def test_a_canned_problem_carries_the_stable_code_only_when_the_test_named_one() -> None:
    assert problem(500, detail="it broke").json() == {
        "status": 500,
        "title": "Problem",
        "detail": "it broke",
    }
    assert (
        problem(502, code="credential-unavailable")
        .json()["type"]
        .endswith("/credential-unavailable")
    )


def test_a_path_segment_that_arrived_from_somewhere_else_cannot_address_another_route() -> None:
    assert segment("../admin") == "..%2Fadmin"
    assert segment("personal") == "personal"


def test_only_the_parameters_that_were_given_are_sent_because_null_means_clear_this() -> None:
    assert given(limit=5, profile=None, q="tea") == {"limit": 5, "q": "tea"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"name": "Prelude"}, "Prelude"), ({"name": None}, ""), ({}, ""), ("not a document", "")],
)
def test_a_text_field_that_never_arrived_reads_as_absent_rather_than_as_the_word_none(
    payload: Any, expected: str
) -> None:
    assert text(payload, "name") == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"count": 3}, 3), ({"count": "3"}, 3), ({"count": "many"}, 0), ({}, 0)],
)
def test_a_number_that_will_not_convert_is_the_default_rather_than_an_exception(
    payload: Any, expected: int
) -> None:
    assert number(payload, "count") == expected


def test_a_boolean_that_is_absent_takes_the_default_which_is_not_always_false() -> None:
    assert flag({"is_playing": True}, "is_playing") is True
    assert flag({}, "is_playing") is False
    assert flag({}, "network", default=True) is True


def test_a_list_field_that_is_missing_or_malformed_is_no_rows_and_never_none() -> None:
    assert rows({"items": [1, 2]}, "items") == (1, 2)
    assert rows({"items": "nope"}, "items") == ()


def test_a_nested_object_is_always_an_object_so_a_chain_of_reads_never_checks_for_null() -> None:
    assert nested({"album": {"name": "Kind of Blue"}}, "album") == {"name": "Kind of Blue"}
    assert nested({"album": None}, "album") == {}
    assert text(nested({}, "album"), "name") == ""


def test_raw_payload_values_are_read_as_given_when_nothing_should_coerce_them() -> None:
    assert field({"value": [1, 2]}, "value") == [1, 2]
    assert field("not a document", "value") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1767225600.0, datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-01-01T00:00:00Z", datetime(2026, 1, 1, tzinfo=UTC)),
        ("2026-01-01T00:00:00", datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_a_timestamp_is_read_from_either_shape_the_family_stores_it_in(
    value: Any, expected: datetime
) -> None:
    assert moment(value) == expected


@pytest.mark.parametrize("value", [None, True, "", "yesterday", ["2026-01-01"]])
def test_anything_that_is_not_a_timestamp_reads_as_no_timestamp(value: Any) -> None:
    assert moment(value) is None


def test_a_provenance_word_this_hub_does_not_recognise_fails_closed_to_untrusted() -> None:
    known = {"stated": Trust.stated}

    assert trust_from("stated", known) is Trust.stated
    assert trust_from("vouched-for-by-a-web-page", known) is Trust.untrusted
    assert trust_from(None, known) is Trust.untrusted


def test_a_capped_list_carries_the_exact_counts_and_an_uncut_one_confesses_nothing() -> None:
    kept, notice = capped([1, 2, 3, 4], 2, "items")
    assert (kept, notice) == ((1, 2), "showing 2 of 4 items")

    kept, notice = capped([1, 2], 2, "items")
    assert (kept, notice) == ((1, 2), "")
