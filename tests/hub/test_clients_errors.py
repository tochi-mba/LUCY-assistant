"""What each status a sibling can answer with has to become.

Two of these translations are load-bearing and the rest are bookkeeping. A 502 naming a
missing credential must become "not connected" -- the thing that offers a person a link
instead of an apology -- and a 429 must carry the interval the service gave and nothing
else, because an invented interval is how one service's rate limit becomes an outage.

The rest of the file is here so that a status nobody thought about cannot quietly become a
success: every branch answers with something named.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from lucy_api.clients.errors import (
    AbsentError,
    ConflictError,
    DownstreamError,
    ForbiddenError,
    NotConnectedError,
    PreconditionError,
    RateLimitedError,
    ReauthenticationError,
    RejectedError,
    UnavailableError,
    problem_code,
    problem_detail,
    raise_for,
    retry_after,
)
from lucy_api.clients.testing import Answer, problem

SERVICE = "spotify"


class Undecodable:
    """A response whose body is not JSON, which is what a proxy in front of a service sends."""

    status_code = 503
    headers: ClassVar[dict[str, str]] = {}

    def json(self) -> Any:
        message = "not json"
        raise ValueError(message)


def test_a_successful_answer_raises_nothing_at_all() -> None:
    raise_for(Answer(status_code=200, body={"ok": True}), service=SERVICE)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ReauthenticationError),
        (403, ForbiddenError),
        (404, AbsentError),
        (409, ConflictError),
        (412, PreconditionError),
        (422, RejectedError),
    ],
)
def test_each_named_status_becomes_the_exception_that_says_what_to_do(
    status: int, expected: type[DownstreamError]
) -> None:
    with pytest.raises(expected) as caught:
        raise_for(problem(status, detail="say why"), service=SERVICE)

    assert caught.value.status == status
    assert caught.value.service == SERVICE
    assert caught.value.detail == "say why"


def test_a_502_naming_a_missing_credential_means_not_connected_rather_than_an_outage() -> None:
    with pytest.raises(NotConnectedError):
        raise_for(problem(502, code="credential-unavailable"), service=SERVICE)


def test_the_underscore_spelling_of_the_credential_code_means_the_same_thing() -> None:
    with pytest.raises(NotConnectedError):
        raise_for(problem(502, code="credential_missing"), service="media")


def test_a_502_from_a_service_that_is_simply_broken_is_an_outage_not_a_disconnection() -> None:
    with pytest.raises(UnavailableError):
        raise_for(problem(502, code="upstream-refused"), service=SERVICE)


def test_a_503_is_an_outage_and_keeps_the_capability() -> None:
    with pytest.raises(UnavailableError):
        raise_for(problem(503, detail="restarting"), service=SERVICE)


def test_a_500_nobody_named_is_still_an_outage() -> None:
    with pytest.raises(UnavailableError):
        raise_for(Answer(status_code=500), service=SERVICE)


def test_a_4xx_nobody_named_is_a_request_this_hub_got_wrong() -> None:
    with pytest.raises(RejectedError):
        raise_for(problem(400, detail="malformed"), service=SERVICE)


def test_a_rate_limit_carries_the_interval_the_service_actually_gave() -> None:
    answer = problem(429, detail="slow down", **{"Retry-After": "30"})

    with pytest.raises(RateLimitedError) as caught:
        raise_for(answer, service=SERVICE)

    assert caught.value.retry_after == 30.0


def test_a_rate_limit_without_a_header_has_no_interval_rather_than_an_invented_one() -> None:
    with pytest.raises(RateLimitedError) as caught:
        raise_for(problem(429), service=SERVICE)

    assert caught.value.retry_after is None


def test_an_unreadable_body_still_produces_the_error_the_status_calls_for() -> None:
    with pytest.raises(UnavailableError) as caught:
        raise_for(Undecodable(), service=SERVICE)

    assert caught.value.detail == ""


def test_the_message_names_the_service_and_the_status_and_quotes_the_detail() -> None:
    assert (
        str(DownstreamError(SERVICE, 409, "at your limit")) == "spotify answered 409: at your limit"
    )
    assert str(DownstreamError(SERVICE, 409)) == "spotify answered 409"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"code": "credential_missing"}, "credential-missing"),
        ({"error": {"code": "Slow_Down"}}, "slow-down"),
        ({"type": "https://x.invalid/problems/no-active-device"}, "no-active-device"),
        ({"title": "Conflict"}, ""),
        ("not a document", ""),
    ],
)
def test_the_stable_code_is_read_from_whichever_shape_the_service_used(
    body: Any, expected: str
) -> None:
    assert problem_code(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": {"message": "the grant expired"}}, "the grant expired"),
        ({"error": {"code": "x"}, "detail": "the grant expired"}, "the grant expired"),
        ({"detail": "the grant expired"}, "the grant expired"),
        ({"title": "Conflict"}, "Conflict"),
        ({}, ""),
        (["not a document"], ""),
    ],
)
def test_the_detail_is_whichever_sentence_the_service_wrote(body: Any, expected: str) -> None:
    assert problem_detail(body) == expected


def test_retry_after_is_read_from_the_header_whichever_case_it_arrived_in() -> None:
    assert retry_after({"retry-after": "5"}) == 5.0
    assert retry_after({"Retry-After": "5"}) == 5.0
    assert retry_after({}) is None


def test_a_retry_after_date_is_resolved_against_the_clock_because_that_is_all_it_can_mean() -> None:
    later = datetime.now(UTC) + timedelta(seconds=120)
    header = later.strftime("%a, %d %b %Y %H:%M:%S GMT")

    seconds = retry_after({"Retry-After": header})

    assert seconds is not None
    assert 0 < seconds <= 120


def test_a_retry_after_date_that_has_already_passed_is_zero_and_never_negative() -> None:
    assert retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0


def test_a_retry_after_date_without_a_zone_is_read_as_utc_rather_than_raising() -> None:
    assert retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 -0000"}) == 0.0


def test_a_retry_after_this_client_cannot_parse_is_no_interval_at_all() -> None:
    assert retry_after({"Retry-After": "soon"}) is None
