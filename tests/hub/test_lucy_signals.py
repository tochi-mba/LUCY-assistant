"""The family's signal client: what a sibling uses to end a subscription.

Two properties matter beyond its own behaviour. Its signatures must be the hub's, byte for
byte -- a sibling that signs one way and a hub that checks another is a feature that never
fires -- and what it sends must be a signal the hub accepts.
"""

from __future__ import annotations

import json

import httpx
import pytest
from lucy_signals import (
    HEADER,
    RETRY_DELAYS_SECONDS,
    Signal,
    deliver,
    sign,
    verify_signature,
)

from lucy_api.net import signing
from lucy_api.work.subscriptions import parse_signal

SECRET = "subscription-secret"
URL = "http://lucy.test/v1/signals/sub_1"


def test_the_client_signs_exactly_as_the_hub_verifies() -> None:
    body = Signal(state="fired", summary="CI is green").body()
    assert HEADER == signing.HEADER
    assert sign(SECRET, body) == signing.sign(SECRET, body)
    assert signing.verify(SECRET, sign(SECRET, body), body)
    assert verify_signature(SECRET, signing.sign(SECRET, body), body)
    assert not verify_signature(SECRET, None, body)
    assert not verify_signature("other", sign(SECRET, body), body)


def test_what_it_sends_is_a_signal_the_hub_accepts_with_its_bounds() -> None:
    signal = Signal(
        state="failed",
        summary="  the   run failed " + "x" * 300,
        facts={"conclusion": "failure", "attempt": 2},
        excerpt="e" * 5000,
    )
    parsed = parse_signal(signal.body())
    assert parsed.state == "failed"
    assert parsed.summary.startswith("the run failed x")
    assert len(parsed.summary) == 120
    assert parsed.facts == {"conclusion": "failure", "attempt": 2}
    assert len(json.loads(signal.body())["excerpt"]) == 1_500


def test_a_bare_signal_sends_only_state_and_summary() -> None:
    assert json.loads(Signal(state="expired", summary="gave up").body()) == {
        "state": "expired",
        "summary": "gave up",
    }


async def no_sleep(seconds: float) -> None:
    del seconds


@pytest.mark.parametrize(("status", "outcome"), [(204, "delivered"), (409, "delivered")])
async def test_a_204_or_a_409_is_delivered_at_the_first_try(status: int, outcome: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await deliver(
            client, url=URL, secret=SECRET, signal=Signal(state="fired", summary="green")
        )

    assert result == outcome
    [request] = seen
    assert request.headers[HEADER] == sign(SECRET, request.content)
    assert request.headers["content-type"] == "application/json"


async def test_a_refusal_is_not_retried_because_it_would_be_refused_again() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await deliver(
            client, url=URL, secret=SECRET, signal=Signal(state="fired", summary="green")
        )

    assert (result, calls) == ("refused", 1)


async def test_a_5xx_or_a_dropped_connection_is_retried_on_the_schedule_then_given_up() -> None:
    answers = iter([httpx.ConnectError("down"), httpx.Response(503), httpx.Response(502)])
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        answer = next(answers, httpx.Response(500))
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def record(seconds: float) -> None:
        slept.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await deliver(
            client,
            url=URL,
            secret=SECRET,
            signal=Signal(state="fired", summary="green"),
            sleep=record,
        )

    assert result == "unreachable"
    assert slept == list(RETRY_DELAYS_SECONDS)


async def test_a_retry_that_lands_is_delivered() -> None:
    answers = iter([httpx.Response(503), httpx.Response(204)])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: next(answers))
    ) as client:
        result = await deliver(
            client,
            url=URL,
            secret=SECRET,
            signal=Signal(state="fired", summary="green"),
            sleep=no_sleep,
        )

    assert result == "delivered"
