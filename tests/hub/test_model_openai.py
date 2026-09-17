"""The Responses API's wire format, against recorded shapes and never against a network.

The same two halves as the Claude suite -- what we send and what we do with what comes
back -- plus one thing that is specific to this provider and easy to get quietly wrong:
cached tokens arrive *inside* `input_tokens` here and beside them everywhere else, so the
test that the two are separated is the test that keeps `Usage` meaning one thing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from lucy_api.model.openai import OpenAIProvider
from lucy_api.model.types import (
    Message,
    ModelRefusedError,
    ModelUnavailableError,
    Request,
    Role,
    Stop,
    Usage,
)
from lucy_api.model.wire import CHUNK_DONE, CHUNK_REASONING, CHUNK_TEXT, ModelCallFailedError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

PLAN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"steps": {"type": "array"}}}
PLAN: dict[str, Any] = {"steps": [{"operation": "notes.search"}]}


def ask(**overrides: Any) -> Request:
    defaults: dict[str, Any] = {"messages": [Message(role=Role.user, content="what day is it")]}
    return Request(**{**defaults, **overrides})


def said(text: str) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def response(**overrides: Any) -> dict[str, Any]:
    """One `response` object, in the shape `POST /v1/responses` returns it."""
    payload: dict[str, Any] = {
        "id": "resp_recorded",
        "object": "response",
        "model": "gpt-5",
        "status": "completed",
        "incomplete_details": None,
        "output": [said("Tuesday.")],
        "usage": {
            "input_tokens": 120,
            "input_tokens_details": {"cached_tokens": 100},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 12},
            "total_tokens": 150,
        },
    }
    return {**payload, **overrides}


def sse(*events: dict[str, Any]) -> str:
    return (
        "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)
        + "data: [DONE]\n\n"
    )


@dataclass(slots=True)
class Fake:
    """One provider wired to canned responses, plus the requests it actually sent."""

    provider: OpenAIProvider
    seen: list[httpx.Request]

    def body(self) -> dict[str, Any]:
        return json.loads(self.seen[-1].content)


@pytest.fixture
async def build() -> AsyncIterator[Callable[..., Fake]]:
    made: list[OpenAIProvider] = []

    def make(*responses: httpx.Response, model: str = "gpt-5") -> Fake:
        seen: list[httpx.Request] = []
        queue = list(responses)

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return queue.pop(0)

        provider = OpenAIProvider(
            api_key="sk-test", model=model, transport=httpx.MockTransport(handle)
        )
        made.append(provider)
        return Fake(provider=provider, seen=seen)

    yield make
    for provider in made:
        await provider.aclose()


async def test_the_call_carries_the_key_as_a_bearer_token(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask())
    assert fake.seen[-1].headers["authorization"] == "Bearer sk-test"
    assert fake.seen[-1].url.path == "/v1/responses"


async def test_the_system_prompt_is_sent_as_instructions_beside_the_conversation(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(system="You are Lucy.", max_output_tokens=512))
    body = fake.body()
    assert body["instructions"] == "You are Lucy."
    assert body["input"] == [{"role": "user", "content": "what day is it"}]
    assert body["max_output_tokens"] == 512


async def test_a_request_with_no_system_prompt_sends_no_instructions_at_all(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask())
    body = fake.body()
    assert "instructions" not in body
    assert "text" not in body
    assert "reasoning" not in body
    assert "temperature" not in body
    assert "stream" not in body


async def test_a_plan_is_asked_for_as_a_named_json_schema_output_format(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert fake.body()["text"] == {
        "format": {"type": "json_schema", "name": "plan", "schema": PLAN_SCHEMA}
    }


async def test_a_thinking_depth_becomes_a_reasoning_effort(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(thinking="low"))
    assert fake.body()["reasoning"] == {"effort": "low"}


async def test_a_depth_deeper_than_this_api_names_is_clamped_rather_than_dropped(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(thinking="max"))
    assert fake.body()["reasoning"] == {"effort": "high"}


async def test_a_thinking_setting_we_do_not_recognise_takes_the_accounts_default(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(thinking="default"))
    assert "reasoning" not in fake.body()


async def test_a_temperature_is_only_sent_when_the_caller_actually_set_one(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(temperature=0.7))
    assert fake.body()["temperature"] == 0.7


async def test_the_request_may_name_a_model_of_its_own_over_the_providers(build) -> None:
    fake = build(httpx.Response(200, json=response()))
    await fake.provider.complete(ask(model="gpt-5-mini"))
    assert fake.body()["model"] == "gpt-5-mini"


async def test_cached_tokens_are_taken_out_of_the_input_count_so_the_two_never_overlap(
    build,
) -> None:
    fake = build(httpx.Response(200, json=response()))
    reply = await fake.provider.complete(ask())
    assert reply.usage == Usage(input_tokens=20, output_tokens=30, cache_read_tokens=100)
    assert reply.text == "Tuesday."
    assert reply.model == "gpt-5"
    assert reply.stop is Stop.end_turn


async def test_a_cached_count_larger_than_the_input_count_never_goes_negative(build) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(usage={"input_tokens": 5, "input_tokens_details": {"cached_tokens": 40}}),
        )
    )
    assert (await fake.provider.complete(ask())).usage.input_tokens == 0


async def test_a_reasoning_summary_is_read_when_the_provider_sends_one_and_never_invented(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(
                output=[
                    {
                        "id": "rs_1",
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "It is a weekday."}],
                    },
                    {"id": "ws_1", "type": "web_search_call", "status": "completed"},
                    said("Tuesday."),
                ]
            ),
        )
    )
    reply = await fake.provider.complete(ask())
    assert reply.reasoning == "It is a weekday."
    assert reply.text == "Tuesday."


async def test_a_content_block_of_a_kind_we_do_not_read_is_ignored_rather_than_fatal(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(
                output=[
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_audio", "data": "..."},
                            {"type": "output_text", "text": "Tuesday."},
                        ],
                    }
                ]
            ),
        )
    )
    assert (await fake.provider.complete(ask())).text == "Tuesday."


async def test_a_plan_comes_back_parsed_and_the_raw_json_is_not_left_for_a_person_to_read(
    build,
) -> None:
    fake = build(httpx.Response(200, json=response(output=[said(json.dumps(PLAN))])))
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.plan == PLAN
    assert reply.text == ""
    assert reply.stop is Stop.tool_use


async def test_a_plan_that_does_not_parse_survives_as_text_so_a_repair_turn_can_quote_it(
    build,
) -> None:
    fake = build(httpx.Response(200, json=response(output=[said('{"steps": [')])))
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.plan is None
    assert reply.text == '{"steps": ['
    assert reply.stop is Stop.end_turn


async def test_a_response_cut_short_stops_for_max_tokens_even_when_it_held_a_whole_plan(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
                output=[said(json.dumps(PLAN))],
            ),
        )
    )
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.stop is Stop.max_tokens, "a truncated plan must not look runnable"
    assert reply.plan == PLAN


async def test_a_refusal_block_raises_rather_than_arriving_as_an_empty_answer(build) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(
                output=[
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "I cannot help with that"}],
                    }
                ]
            ),
        )
    )
    with pytest.raises(ModelRefusedError, match="I cannot help with that"):
        await fake.provider.complete(ask())


async def test_a_content_filter_that_stopped_the_response_is_also_a_refusal(build) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(
                status="incomplete",
                incomplete_details={"reason": "content_filter"},
                output=[],
            ),
        )
    )
    with pytest.raises(ModelRefusedError, match="the content filter stopped the response"):
        await fake.provider.complete(ask())


async def test_a_response_the_provider_marked_failed_is_permanent_and_names_its_code(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=response(
                status="failed",
                error={"code": "server_error", "message": "something went wrong"},
                output=[],
            ),
        )
    )
    with pytest.raises(ModelCallFailedError, match=r"\(server_error\): something went wrong"):
        await fake.provider.complete(ask())


async def test_a_failed_response_with_no_error_object_still_says_which_provider_failed(
    build,
) -> None:
    fake = build(httpx.Response(200, json=response(status="failed", output=[])))
    with pytest.raises(ModelCallFailedError, match=r"\(no code\): no detail given"):
        await fake.provider.complete(ask())


async def test_a_rate_limit_is_retryable_and_carries_the_interval_the_provider_named(
    build,
) -> None:
    fake = build(
        httpx.Response(
            429,
            headers={"retry-after": "4"},
            json={"error": {"message": "rate limit reached", "type": "rate_limit_error"}},
        )
    )
    with pytest.raises(ModelUnavailableError) as raised:
        await fake.provider.complete(ask())
    assert raised.value.retry_after == 4.0


async def test_a_revoked_key_is_permanent_and_must_not_be_retried(build) -> None:
    fake = build(httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}}))
    with pytest.raises(ModelCallFailedError, match="Incorrect API key provided"):
        await fake.provider.complete(ask())


async def test_a_stream_yields_reasoning_and_text_as_they_arrive_then_the_whole_reply(
    build,
) -> None:
    body = sse(
        {"type": "response.created", "response": response(status="in_progress", output=[])},
        {"type": "response.reasoning_summary_text.delta", "delta": "weekday"},
        {"type": "response.output_text.delta", "delta": "Tues"},
        {"type": "response.output_text.delta", "delta": "day."},
        {"type": "response.completed", "response": response()},
    )
    fake = build(httpx.Response(200, text=body))
    chunks = [chunk async for chunk in fake.provider.stream(ask())]
    assert [(chunk.kind, chunk.text) for chunk in chunks[:-1]] == [
        (CHUNK_REASONING, "weekday"),
        (CHUNK_TEXT, "Tues"),
        (CHUNK_TEXT, "day."),
    ]
    done = chunks[-1]
    assert done.kind == CHUNK_DONE
    assert done.reply is not None
    assert done.reply.text == "Tuesday."
    assert fake.body()["stream"] is True


async def test_a_stream_that_ends_incomplete_reports_the_same_stop_the_single_call_would(
    build,
) -> None:
    body = sse(
        {
            "type": "response.incomplete",
            "response": response(
                status="incomplete",
                incomplete_details={"reason": "max_output_tokens"},
                output=[said("Tues")],
            ),
        }
    )
    fake = build(httpx.Response(200, text=body))
    done = [chunk async for chunk in fake.provider.stream(ask())][-1]
    assert done.reply is not None
    assert done.reply.stop is Stop.max_tokens


async def test_a_rate_limit_mid_stream_is_retryable(build) -> None:
    body = sse({"type": "error", "code": "rate_limit_exceeded", "message": "slow down"})
    fake = build(httpx.Response(200, text=body))
    with pytest.raises(ModelUnavailableError, match="rate_limit_exceeded mid-stream"):
        [chunk async for chunk in fake.provider.stream(ask())]


async def test_an_error_mid_stream_with_nothing_in_it_is_permanent_rather_than_retried(
    build,
) -> None:
    fake = build(httpx.Response(200, text=sse({"type": "error"})))
    with pytest.raises(ModelCallFailedError, match="openai sent error mid-stream"):
        [chunk async for chunk in fake.provider.stream(ask())]


async def test_a_streamed_plan_is_parsed_from_the_response_the_terminal_event_carried(
    build,
) -> None:
    raw = json.dumps(PLAN)
    body = sse(
        {"type": "response.output_text.delta", "delta": raw},
        {"type": "response.completed", "response": response(output=[said(raw)])},
    )
    fake = build(httpx.Response(200, text=body))
    done = [chunk async for chunk in fake.provider.stream(ask(plan_schema=PLAN_SCHEMA))][-1]
    assert done.reply is not None
    assert done.reply.plan == PLAN
    assert done.reply.stop is Stop.tool_use
