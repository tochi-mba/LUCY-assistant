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
    plan_object,
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


# --- a plan in a code fence is still a plan -------------------------------------------------
#
# The first real model ever pointed at this hub replied with its plan wrapped in ```json. With
# a bare `json.loads` that read as prose, so the steps never ran and the turn ended as a
# success with the wire format shown to the person as the answer.

FENCED_PLAN = '```json\n{"steps":[{"id":"caps","op":"capabilities.list","input":{}}]}\n```'


def test_a_fenced_plan_is_read_as_a_plan() -> None:
    assert json_object(FENCED_PLAN) == {
        "steps": [{"id": "caps", "op": "capabilities.list", "input": {}}]
    }


def test_a_fence_with_no_language_still_counts() -> None:
    assert json_object('```\n{"a": 1}\n```') == {"a": 1}


def test_a_fence_inside_a_sentence_is_quoting_not_planning() -> None:
    """Pulling the first fenced block out of a message with prose around it would read
    "here is what that config looks like: ..." as a plan and run it."""
    assert json_object('Here you go:\n```json\n{"a": 1}\n```\nHope that helps.') is None


def test_a_fenced_array_is_no_more_an_object_than_a_bare_one() -> None:
    assert json_object("```json\n[1, 2]\n```") is None


def test_a_fenced_mess_still_reaches_the_repair_path() -> None:
    assert json_object("```json\n{not json\n```") is None


def test_carriage_returns_do_not_defeat_the_fence() -> None:
    assert json_object('```json\r\n{"a": 1}\r\n```') == {"a": 1}


def test_surrounding_whitespace_is_tolerated() -> None:
    assert json_object('\n\n```json\n{"a": 1}\n```\n\n') == {"a": 1}


# --- a plan the model put a sentence in front of ------------------------------------------------
#
# Models narrate. This one repeatedly emitted a progress note and then the plan it had already
# decided on, and reading the pair as prose ended the turn -- showing the person the wire
# format and never running the step.

NARRATED = (
    "The step timed out, but the command kept running and finished. "
    "I'm fetching its output now.\n\n"
    '{"steps":[{"id":"pyresult","op":"work.result","input":{"work_id":"wrk_1"}}]}'
)


def test_a_narrated_plan_is_still_a_plan() -> None:
    assert plan_object(NARRATED) == {
        "steps": [{"id": "pyresult", "op": "work.result", "input": {"work_id": "wrk_1"}}]
    }


def test_a_bare_plan_is_read_the_same_way() -> None:
    assert plan_object('{"steps":[{"id":"a"}]}') == {"steps": [{"id": "a"}]}


def test_a_fenced_plan_is_read_the_same_way() -> None:
    assert plan_object('```json\n{"steps":[{"id":"a"}]}\n```') == {"steps": [{"id": "a"}]}


def test_an_answer_that_ends_by_quoting_a_configuration_is_not_a_plan() -> None:
    """The risk this guard exists for. Without the `steps` check, a final answer that ends in
    an object would be executed, fail validation, and burn the turn's repair budget."""
    assert plan_object('Here is your config: {"host": "a", "port": 1}') is None


def test_a_plain_answer_is_not_a_plan() -> None:
    assert plan_object("All done. Nothing else to do.") is None


def test_the_object_has_to_be_the_last_thing_in_the_message() -> None:
    """A plan the model then talked itself out of is not what it finished on."""
    assert plan_object('Maybe {"steps":[1]} -- no, actually {"other": 2}') is None


def test_the_last_object_wins_when_there_are_several() -> None:
    assert plan_object('Note: {"x":1}\n{"steps":[{"id":"z"}]}') == {"steps": [{"id": "z"}]}


def test_json_object_itself_stays_strict() -> None:
    """`plan_object` is the forgiving one. `json_object` reads error bodies and anything else
    that must be JSON or nothing, and loosening it would let a sentence become a payload."""
    assert json_object(NARRATED) is None
