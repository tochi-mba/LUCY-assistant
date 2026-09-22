"""The translations both HTTP providers share.

These are small functions and the tests are correspondingly small, but they decide
whether a rate limit is honoured or hammered, whether a revoked key is retried forever,
and whether a provider that answers with a proxy's HTML error page produces a sentence
anybody can act on. All three are the kind of thing that is only noticed in production.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from lucy_api.model.types import Message, ModelUnavailableError, Request, Role
from lucy_api.model.wire import (
    MESSAGE_LIMIT,
    ModelCallFailedError,
    as_dict,
    as_list,
    as_text,
    check_status,
    count,
    error_message,
    events,
    json_object,
    model_for,
    retry_after_seconds,
    send,
    sse_event,
)

FAR_FUTURE = "Fri, 01 Jan 2100 00:00:00 GMT"
LONG_PAST_WITHOUT_ZONE = "Wed, 21 Oct 2015 07:28:00 -0000"


def ask(**overrides: Any) -> Request:
    defaults: dict[str, Any] = {"messages": [Message(role=Role.user, content="hello")]}
    return Request(**{**defaults, **overrides})


async def client_over(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(handler))


def test_a_field_the_provider_left_out_reads_as_empty_rather_than_as_a_crash() -> None:
    assert as_dict({"a": 1}) == {"a": 1}
    assert as_dict(None) == {}
    assert as_list([1]) == [1]
    assert as_list("nope") == []
    assert as_text("said") == "said"
    assert as_text(None) == ""
    assert count(12) == 12
    assert count(None) == 0


def test_only_a_json_object_counts_as_one() -> None:
    assert json_object('{"a": 1}') == {"a": 1}
    assert json_object("[1, 2]") is None, "an array is not the object we asked for"
    assert json_object("{not json") is None


def test_a_request_may_name_its_own_model_and_otherwise_takes_the_providers() -> None:
    assert model_for(ask(model="claude-opus-5"), "gpt-5") == "claude-opus-5"
    assert model_for(ask(), "gpt-5") == "gpt-5"


def test_a_call_with_no_model_anywhere_says_where_a_model_name_comes_from() -> None:
    with pytest.raises(ModelCallFailedError, match="provider:model"):
        model_for(ask(), "")


def test_no_retry_after_header_means_no_interval_rather_than_a_default_one() -> None:
    assert retry_after_seconds(httpx.Headers({})) is None


def test_retry_after_in_seconds_is_obeyed_exactly_and_case_insensitively() -> None:
    assert retry_after_seconds(httpx.Headers({"Retry-After": " 7 "})) == 7.0


def test_a_retry_after_already_in_the_past_is_zero_rather_than_negative() -> None:
    assert retry_after_seconds(httpx.Headers({"retry-after": "-5"})) == 0.0


def test_retry_after_as_an_http_date_becomes_the_seconds_until_then() -> None:
    seconds = retry_after_seconds(httpx.Headers({"retry-after": FAR_FUTURE}))
    assert seconds is not None
    assert seconds > 0.0


def test_an_http_date_with_no_zone_is_read_as_utc_rather_than_discarded() -> None:
    assert retry_after_seconds(httpx.Headers({"retry-after": LONG_PAST_WITHOUT_ZONE})) == 0.0


def test_a_retry_after_we_cannot_read_is_absent_rather_than_guessed_at() -> None:
    assert retry_after_seconds(httpx.Headers({"retry-after": "in a bit"})) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"error": {"message": " slow down "}}', "slow down"),
        ('{"error": {"message": ""}}', '{"error": {"message": ""}}'),
        ('{"error": "boom"}', '{"error": "boom"}'),
        ('{"ok": true}', '{"ok": true}'),
        ("<html>\n  Bad Gateway\n</html>", "<html> Bad Gateway </html>"),
    ],
)
def test_the_providers_own_sentence_is_preferred_and_the_body_is_the_fallback(
    body, expected
) -> None:
    assert error_message(body, fallback="no detail given") == expected


def test_an_empty_body_falls_back_to_the_sentence_we_wrote() -> None:
    assert error_message("", fallback="no detail given") == "no detail given"


def test_a_providers_sentence_is_bounded_like_the_fallback_is() -> None:
    """The long one is the one echoing the request back, and that must not reach a log."""
    body = json.dumps({"error": {"message": "you sent: " + "x" * 2_000}})
    message = error_message(body, fallback="no detail given")
    assert len(message) == MESSAGE_LIMIT
    assert message.startswith("you sent: ")


async def test_a_transport_failure_never_carries_the_url() -> None:
    """For a provider that authenticates in the query string, the URL is the credential."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    http = await client_over(refuse)
    request = http.build_request("POST", "/v1/models:generate?key=sk-live-SECRET", json={})
    with pytest.raises(ModelUnavailableError) as raised:
        await send(http, request, provider="gemini")
    await http.aclose()

    assert str(raised.value) == "gemini could not be reached (ConnectTimeout)"
    assert "SECRET" not in str(raised.value)


def test_a_good_status_raises_nothing() -> None:
    check_status(provider="anthropic", status=200, headers=httpx.Headers({}), body="")


def test_a_rate_limit_is_retryable_and_carries_the_interval_it_was_given() -> None:
    with pytest.raises(ModelUnavailableError) as raised:
        check_status(
            provider="anthropic",
            status=429,
            headers=httpx.Headers({"retry-after": "3"}),
            body='{"error": {"message": "rate limited"}}',
        )
    assert raised.value.retry_after == 3.0
    assert "anthropic answered 429: rate limited" in str(raised.value)


def test_a_server_error_is_retryable_even_without_an_interval() -> None:
    with pytest.raises(ModelUnavailableError) as raised:
        check_status(provider="openai", status=503, headers=httpx.Headers({}), body="")
    assert raised.value.retry_after is None


def test_a_bad_request_is_not_retryable_because_repeating_it_fails_the_same_way() -> None:
    with pytest.raises(ModelCallFailedError, match="openai answered 400: bad schema"):
        check_status(
            provider="openai",
            status=400,
            headers=httpx.Headers({}),
            body='{"error": {"message": "bad schema"}}',
        )


def test_only_a_data_line_carrying_json_is_an_event() -> None:
    assert sse_event('data: {"type": "ping"}') == {"type": "ping"}
    assert sse_event("event: ping") is None
    assert sse_event("data: [DONE]") is None, "the sentinel is a word, not an object"


async def test_a_call_that_never_reached_the_provider_is_worth_retrying() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    http = await client_over(refuse)
    request = http.build_request("POST", "/v1/messages", json={})
    with pytest.raises(ModelUnavailableError, match="anthropic could not be reached"):
        await send(http, request, provider="anthropic")
    await http.aclose()


async def test_a_two_hundred_that_is_not_json_is_a_failure_rather_than_an_empty_reply() -> None:
    http = await client_over(lambda _: httpx.Response(200, text="<html>hello</html>"))
    request = http.build_request("POST", "/v1/messages", json={})
    with pytest.raises(ModelCallFailedError, match="not JSON"):
        await send(http, request, provider="anthropic")
    await http.aclose()


async def test_a_good_call_comes_back_as_the_object_the_provider_sent() -> None:
    http = await client_over(lambda _: httpx.Response(200, json={"id": "msg_1"}))
    request = http.build_request("POST", "/v1/messages", json={})
    assert await send(http, request, provider="anthropic") == {"id": "msg_1"}
    await http.aclose()


async def test_a_stream_yields_its_events_and_ignores_the_framing_around_them() -> None:
    body = (
        "event: one\n"
        'data: {"type": "one"}\n'
        "\n"
        ": keep-alive\n"
        "data: not json\n"
        "\n"
        'data: {"type": "two"}\n'
        "\n"
    )
    http = await client_over(lambda _: httpx.Response(200, text=body))
    request = http.build_request("POST", "/v1/messages", json={})
    seen = [event async for event in events(http, request, provider="anthropic")]
    assert seen == [{"type": "one"}, {"type": "two"}]
    await http.aclose()


async def test_a_stream_that_began_with_a_rate_limit_raises_instead_of_ending_silently() -> None:
    http = await client_over(
        lambda _: httpx.Response(429, headers={"retry-after": "2"}, json={"error": {}})
    )
    request = http.build_request("POST", "/v1/messages", json={})
    with pytest.raises(ModelUnavailableError) as raised:
        [event async for event in events(http, request, provider="anthropic")]
    assert raised.value.retry_after == 2.0
    await http.aclose()


async def test_a_stream_that_could_not_be_opened_is_worth_retrying() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("took too long", request=request)

    http = await client_over(refuse)
    request = http.build_request("POST", "/v1/messages", json={})
    with pytest.raises(ModelUnavailableError, match="could not be reached"):
        [event async for event in events(http, request, provider="openai")]
    await http.aclose()
