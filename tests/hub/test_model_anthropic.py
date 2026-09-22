"""Claude's wire format, against recorded shapes and never against a network.

Every payload here is the shape `POST /v1/messages` actually answers with, trimmed to
the fields this adapter reads. The assertions come in two halves, and both matter: what
we *send* -- because a system prompt in the wrong place is the difference between a
cached prefix and a full-price one every turn -- and what we do with what comes back,
because `stop_reason` decides whether a client offers to resume something that cannot be.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from lucy_api.model.anthropic import API_VERSION, AnthropicProvider
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


def message(**overrides: Any) -> dict[str, Any]:
    """One `message` object, in the shape the Messages API returns it."""
    payload: dict[str, Any] = {
        "id": "msg_01Recorded",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "Tuesday."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 30,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    return {**payload, **overrides}


def sse(*events: tuple[str, dict[str, Any]]) -> str:
    return "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)


@dataclass(slots=True)
class Fake:
    """One provider wired to canned responses, plus the requests it actually sent."""

    provider: AnthropicProvider
    seen: list[httpx.Request]

    def body(self) -> dict[str, Any]:
        return json.loads(self.seen[-1].content)


@pytest.fixture
async def build() -> AsyncIterator[Callable[..., Fake]]:
    made: list[AnthropicProvider] = []

    def make(*responses: httpx.Response, model: str = "claude-opus-5") -> Fake:
        seen: list[httpx.Request] = []
        queue = list(responses)

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return queue.pop(0)

        provider = AnthropicProvider(
            api_key="sk-ant-test", model=model, transport=httpx.MockTransport(handle)
        )
        made.append(provider)
        return Fake(provider=provider, seen=seen)

    yield make
    for provider in made:
        await provider.aclose()


async def test_the_call_carries_the_api_key_and_the_version_the_wire_format_is_pinned_to(
    build,
) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask())
    assert fake.seen[-1].headers["x-api-key"] == "sk-ant-test"
    assert fake.seen[-1].headers["anthropic-version"] == API_VERSION
    assert fake.seen[-1].url.path == "/v1/messages"


async def test_the_system_prompt_is_sent_beside_the_messages_and_marked_as_the_cache_prefix(
    build,
) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(system="You are Lucy."))
    body = fake.body()
    assert body["system"] == [
        {
            "type": "text",
            "text": "You are Lucy.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert body["messages"] == [{"role": "user", "content": "what day is it"}]


async def test_a_request_with_no_system_prompt_sends_no_system_field_at_all(build) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(max_output_tokens=512))
    body = fake.body()
    assert "system" not in body
    assert body["max_tokens"] == 512
    assert "output_config" not in body
    assert "thinking" not in body
    assert "temperature" not in body
    assert "stream" not in body


async def test_a_plan_is_asked_for_as_structured_output_against_the_schema_it_was_given(
    build,
) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert fake.body()["output_config"] == {
        "format": {"type": "json_schema", "schema": PLAN_SCHEMA}
    }


async def test_a_thinking_depth_becomes_adaptive_thinking_and_an_effort_level(build) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(thinking="high"))
    body = fake.body()
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "high"}


async def test_a_thinking_token_budget_is_an_explicit_ceiling_not_an_effort_level(build) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(thinking="high", max_thinking_tokens=1_024))
    body = fake.body()
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 1_024}
    assert "output_config" not in body


async def test_thinking_switched_off_is_said_explicitly_because_it_is_on_by_default(
    build,
) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(thinking="off"))
    assert fake.body()["thinking"] == {"type": "disabled"}


async def test_a_thinking_setting_we_do_not_recognise_says_nothing_rather_than_guessing(
    build,
) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(thinking="default"))
    assert "thinking" not in fake.body()


async def test_a_temperature_is_only_sent_when_the_caller_actually_set_one(build) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(temperature=0.2))
    assert fake.body()["temperature"] == 0.2


async def test_the_request_may_name_a_model_of_its_own_over_the_providers(build) -> None:
    fake = build(httpx.Response(200, json=message()))
    await fake.provider.complete(ask(model="claude-haiku-4-5"))
    assert fake.body()["model"] == "claude-haiku-4-5"


async def test_a_reply_keeps_text_reasoning_and_the_cache_tokens_reported_beside_the_input(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=message(
                content=[
                    {"type": "thinking", "thinking": "It is a weekday."},
                    {"type": "redacted_thinking", "data": "opaque"},
                    {"type": "text", "text": "Tuesday."},
                ],
                usage={
                    "input_tokens": 12,
                    "output_tokens": 30,
                    "cache_creation_input_tokens": 400,
                    "cache_read_input_tokens": 9_000,
                },
            ),
        )
    )
    reply = await fake.provider.complete(ask())
    assert reply.text == "Tuesday."
    assert reply.reasoning == "It is a weekday."
    assert reply.stop is Stop.end_turn
    assert reply.model == "claude-opus-5"
    assert reply.usage == Usage(
        input_tokens=12, output_tokens=30, cache_read_tokens=9_000, cache_write_tokens=400
    )


async def test_a_plan_comes_back_parsed_and_the_raw_json_is_not_left_for_a_person_to_read(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=message(
                content=[{"type": "text", "text": json.dumps(PLAN)}], stop_reason="tool_use"
            ),
        )
    )
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.plan == PLAN
    assert reply.text == ""
    assert reply.stop is Stop.tool_use


async def test_a_plan_that_does_not_parse_survives_as_text_so_a_repair_turn_can_quote_it(
    build,
) -> None:
    fake = build(
        httpx.Response(200, json=message(content=[{"type": "text", "text": '{"steps": ['}]))
    )
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.plan is None
    assert reply.text == '{"steps": ['
    assert reply.stop is Stop.end_turn


async def test_a_reply_cut_short_stops_for_max_tokens_even_when_it_held_a_whole_plan(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=message(
                content=[{"type": "text", "text": json.dumps(PLAN)}], stop_reason="max_tokens"
            ),
        )
    )
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.stop is Stop.max_tokens, "a truncated plan must not look runnable"
    assert reply.plan == PLAN


async def test_an_unknown_stop_reason_ends_the_turn_rather_than_inventing_resumability(
    build,
) -> None:
    fake = build(httpx.Response(200, json=message(stop_reason="something_new")))
    assert (await fake.provider.complete(ask())).stop is Stop.end_turn


async def test_a_missing_stop_reason_also_ends_the_turn(build) -> None:
    fake = build(httpx.Response(200, json=message(stop_reason=None)))
    assert (await fake.provider.complete(ask())).stop is Stop.end_turn


async def test_a_refusal_raises_and_repeats_the_category_and_explanation_it_was_given(
    build,
) -> None:
    fake = build(
        httpx.Response(
            200,
            json=message(
                stop_reason="refusal",
                stop_details={
                    "type": "refusal",
                    "category": "cyber",
                    "explanation": "this would build an exploit",
                },
            ),
        )
    )
    with pytest.raises(ModelRefusedError, match=r"\(cyber\): this would build an exploit"):
        await fake.provider.complete(ask())


async def test_a_refusal_with_no_details_still_says_who_declined(build) -> None:
    fake = build(httpx.Response(200, json=message(stop_reason="refusal")))
    with pytest.raises(ModelRefusedError, match=r"\(unspecified\): no reason was given"):
        await fake.provider.complete(ask())


async def test_a_rate_limit_is_retryable_and_carries_the_interval_the_provider_named(
    build,
) -> None:
    fake = build(
        httpx.Response(
            429,
            headers={"retry-after": "12"},
            json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
        )
    )
    with pytest.raises(ModelUnavailableError) as raised:
        await fake.provider.complete(ask())
    assert raised.value.retry_after == 12.0


async def test_an_overloaded_provider_is_retryable(build) -> None:
    fake = build(
        httpx.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )
    )
    with pytest.raises(ModelUnavailableError, match="anthropic answered 529: busy"):
        await fake.provider.complete(ask())


async def test_a_schema_the_provider_refuses_is_permanent_and_must_not_be_retried(
    build,
) -> None:
    fake = build(
        httpx.Response(
            400,
            json={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "schema too deep"},
            },
        )
    )
    with pytest.raises(ModelCallFailedError, match="schema too deep"):
        await fake.provider.complete(ask())


async def test_a_stream_yields_thinking_and_text_as_they_arrive_then_the_whole_reply(
    build,
) -> None:
    body = sse(
        ("message_start", {"type": "message_start", "message": message(content=[])}),
        (
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "weekday"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "abc"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("ping", {"type": "ping"}),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Tues"},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "day."},
            },
        ),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 30},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
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
    assert done.reply.reasoning == "weekday"
    assert done.reply.stop is Stop.end_turn
    assert done.reply.usage == Usage(input_tokens=120, output_tokens=30)
    assert fake.body()["stream"] is True


async def test_a_streamed_plan_is_parsed_from_the_pieces_it_arrived_in(build) -> None:
    raw = json.dumps(PLAN)
    body = sse(
        ("message_start", {"type": "message_start", "message": message(content=[])}),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": raw[:8]}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": raw[8:]}},
        ),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
        ("message_stop", {"type": "message_stop"}),
    )
    fake = build(httpx.Response(200, text=body))
    done = [chunk async for chunk in fake.provider.stream(ask(plan_schema=PLAN_SCHEMA))][-1]
    assert done.reply is not None
    assert done.reply.plan == PLAN
    assert done.reply.stop is Stop.tool_use


async def test_an_overloaded_error_mid_stream_is_retryable(build) -> None:
    body = sse(
        (
            "error",
            {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "try again shortly"},
            },
        )
    )
    fake = build(httpx.Response(200, text=body))
    with pytest.raises(ModelUnavailableError, match="overloaded_error mid-stream"):
        [chunk async for chunk in fake.provider.stream(ask())]


async def test_an_error_mid_stream_with_nothing_in_it_is_permanent_rather_than_retried(
    build,
) -> None:
    fake = build(httpx.Response(200, text=sse(("error", {"type": "error"}))))
    with pytest.raises(ModelCallFailedError, match="anthropic sent error mid-stream"):
        [chunk async for chunk in fake.provider.stream(ask())]


async def test_a_refusal_that_only_appears_at_the_end_of_a_stream_still_raises(build) -> None:
    body = sse(
        ("message_start", {"type": "message_start", "message": message(content=[])}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "refusal",
                    "stop_details": {"category": "bio", "explanation": "no"},
                },
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    fake = build(httpx.Response(200, text=body))
    with pytest.raises(ModelRefusedError, match=r"\(bio\): no"):
        [chunk async for chunk in fake.provider.stream(ask())]
