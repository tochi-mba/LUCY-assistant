"""The chat-completions dialect, against recorded shapes and never against a network.

One adapter speaks for forty providers, so what is pinned here is the dialect and the
deviations the catalogue declares: which header carries the key, how a plan is asked for
when the provider can and cannot take a schema, where the model's thinking arrives, and
that cached tokens are taken out of the prompt count so `Usage` means one thing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from lucy_api.model.catalogue import Auth, Binding, ProviderSpec, Traits, spec_for
from lucy_api.model.chat import ChatProvider
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


def completion(**overrides: Any) -> dict[str, Any]:
    """One completion, in the shape `POST /chat/completions` returns it."""
    payload: dict[str, Any] = {
        "id": "chatcmpl_recorded",
        "object": "chat.completion",
        "model": "deepseek-v4-pro",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Tuesday."},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens": 30,
            "total_tokens": 150,
        },
    }
    return {**payload, **overrides}


def chunk(delta: dict[str, Any], *, finish: str | None = None, **extra: Any) -> dict[str, Any]:
    return {
        "id": "chatcmpl_recorded",
        "object": "chat.completion.chunk",
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        **extra,
    }


def sse(*events: dict[str, Any]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def a_spec(**overrides: Any) -> ProviderSpec:
    fields: dict[str, Any] = {
        "id": "example",
        "title": "Example",
        "base_url": "https://api.example.invalid/v1",
    }
    return ProviderSpec(**{**fields, **overrides})


@dataclass(slots=True)
class Fake:
    provider: ChatProvider
    seen: list[httpx.Request]

    def body(self) -> dict[str, Any]:
        return json.loads(self.seen[-1].content)


@pytest.fixture
async def build() -> AsyncIterator[Callable[..., Fake]]:
    made: list[ChatProvider] = []

    def make(
        *responses: httpx.Response, spec: ProviderSpec | None = None, base_url: str = ""
    ) -> Fake:
        seen: list[httpx.Request] = []
        queue = list(responses)

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return queue.pop(0)

        provider = ChatProvider(
            Binding(spec or a_spec(), api_key="sk-test", base_url=base_url),
            "deepseek-v4-pro",
            transport=httpx.MockTransport(handle),
        )
        made.append(provider)
        return Fake(provider=provider, seen=seen)

    yield make
    for provider in made:
        await provider.aclose()


# --------------------------------------------------------------------------------------
# What we send
# --------------------------------------------------------------------------------------


async def test_the_key_travels_the_way_the_catalogue_row_says(build) -> None:
    bearer = build(httpx.Response(200, json=completion()))
    await bearer.provider.complete(ask())
    assert bearer.seen[-1].headers["authorization"] == "Bearer sk-test"
    assert bearer.seen[-1].url == "https://api.example.invalid/v1/chat/completions"

    header = build(httpx.Response(200, json=completion()), spec=a_spec(auth=Auth.api_key_header))
    await header.provider.complete(ask())
    assert header.seen[-1].headers["api-key"] == "sk-test"
    assert "authorization" not in header.seen[-1].headers

    x_key = build(httpx.Response(200, json=completion()), spec=a_spec(auth=Auth.x_api_key))
    await x_key.provider.complete(ask())
    assert x_key.seen[-1].headers["x-api-key"] == "sk-test"


async def test_a_local_runtime_sends_no_credential_at_all(build) -> None:
    fake = build(httpx.Response(200, json=completion()), spec=a_spec(auth=Auth.none))
    await fake.provider.complete(ask())
    headers = fake.seen[-1].headers
    assert "authorization" not in headers
    assert "x-api-key" not in headers
    assert "api-key" not in headers


async def test_a_deployment_base_url_overrides_the_catalogue_one(build) -> None:
    """A cloud tenant and a runtime on another port are facts about this machine."""
    fake = build(httpx.Response(200, json=completion()), base_url="http://127.0.0.1:9000/v1")
    await fake.provider.complete(ask())
    assert fake.seen[-1].url == "http://127.0.0.1:9000/v1/chat/completions"


async def test_the_system_prompt_leads_the_messages_and_max_tokens_is_always_sent(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    await fake.provider.complete(ask(system="You are Lucy.", max_output_tokens=512))
    body = fake.body()
    assert body["messages"] == [
        {"role": "system", "content": "You are Lucy."},
        {"role": "user", "content": "what day is it"},
    ]
    assert body["max_tokens"] == 512
    assert body["model"] == "deepseek-v4-pro"


async def test_a_bare_request_sends_nothing_it_was_not_asked_for(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    await fake.provider.complete(ask())
    body = fake.body()
    assert body["messages"][0]["role"] == "user"
    for absent in ("response_format", "reasoning_effort", "temperature", "stream"):
        assert absent not in body


async def test_a_plan_is_asked_for_as_a_schema_where_the_provider_takes_one(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert fake.body()["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "plan", "schema": PLAN_SCHEMA},
    }


async def test_a_plan_through_a_json_only_provider_puts_the_schema_in_words(build) -> None:
    """DeepSeek and Moonshot understand "answer in JSON" and nothing finer."""
    fake = build(
        httpx.Response(200, json=completion()),
        spec=a_spec(traits=Traits(json_schema=False)),
    )
    await fake.provider.complete(ask(system="You are Lucy.", plan_schema=PLAN_SCHEMA))
    body = fake.body()
    assert body["response_format"] == {"type": "json_object"}
    system = body["messages"][0]["content"]
    assert system.startswith("You are Lucy.")
    assert json.dumps(PLAN_SCHEMA) in system


async def test_a_thinking_depth_becomes_a_reasoning_effort_where_it_is_understood(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    await fake.provider.complete(ask(thinking="xhigh"))
    assert fake.body()["reasoning_effort"] == "high", "clamped to the deepest named level"

    silent = build(
        httpx.Response(200, json=completion()),
        spec=a_spec(traits=Traits(reasoning_effort=False)),
    )
    await silent.provider.complete(ask(thinking="xhigh"))
    assert "reasoning_effort" not in silent.body()


async def test_temperature_is_passed_through_when_set(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    await fake.provider.complete(ask(temperature=0.2))
    assert fake.body()["temperature"] == 0.2


async def test_a_request_model_overrides_the_bound_one(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    await fake.provider.complete(ask(model="deepseek-flash"))
    assert fake.body()["model"] == "deepseek-flash"


# --------------------------------------------------------------------------------------
# What we do with what comes back
# --------------------------------------------------------------------------------------


async def test_a_spoken_reply_is_text_with_cached_tokens_taken_out_of_the_input(build) -> None:
    fake = build(httpx.Response(200, json=completion()))
    reply = await fake.provider.complete(ask())
    assert reply.text == "Tuesday."
    assert reply.plan is None
    assert reply.stop is Stop.end_turn
    assert reply.model == "deepseek-v4-pro"
    assert reply.usage == Usage(input_tokens=20, output_tokens=30, cache_read_tokens=100)


async def test_content_may_arrive_as_a_list_of_parts(build) -> None:
    parts = [{"type": "text", "text": "Tues"}, {"type": "text", "text": "day."}]
    fake = build(
        httpx.Response(
            200,
            json=completion(
                choices=[{"message": {"content": parts}, "finish_reason": "stop"}],
            ),
        )
    )
    assert (await fake.provider.complete(ask())).text == "Tuesday."


async def test_the_models_thinking_is_read_from_either_field_it_arrives_in(build) -> None:
    deepseek = build(
        httpx.Response(
            200,
            json=completion(
                choices=[
                    {
                        "message": {"content": "Tuesday.", "reasoning_content": "It is the 16th."},
                        "finish_reason": "stop",
                    }
                ]
            ),
        )
    )
    assert (await deepseek.provider.complete(ask())).reasoning == "It is the 16th."

    other = build(
        httpx.Response(
            200,
            json=completion(
                choices=[
                    {
                        "message": {"content": "Tuesday.", "reasoning": "Counted from Monday."},
                        "finish_reason": "stop",
                    }
                ]
            ),
        )
    )
    assert (await other.provider.complete(ask())).reasoning == "Counted from Monday."


async def test_a_plan_that_parses_is_returned_as_a_plan_and_not_as_text(build) -> None:
    fake = build(
        httpx.Response(
            200,
            json=completion(
                choices=[{"message": {"content": json.dumps(PLAN)}, "finish_reason": "stop"}]
            ),
        )
    )
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.plan == PLAN
    assert reply.text == ""
    assert reply.stop is Stop.tool_use


async def test_a_plan_that_does_not_parse_keeps_its_text_for_the_repair_round(build) -> None:
    fake = build(
        httpx.Response(
            200,
            json=completion(
                choices=[{"message": {"content": "not json"}, "finish_reason": "stop"}]
            ),
        )
    )
    reply = await fake.provider.complete(ask(plan_schema=PLAN_SCHEMA))
    assert reply.plan is None
    assert reply.text == "not json"


async def test_running_out_of_room_is_the_resumable_stop(build) -> None:
    fake = build(
        httpx.Response(
            200,
            json=completion(choices=[{"message": {"content": "Tues"}, "finish_reason": "length"}]),
        )
    )
    assert (await fake.provider.complete(ask())).stop is Stop.max_tokens


async def test_a_refusal_field_and_a_content_filter_are_both_refusals(build) -> None:
    refused = build(
        httpx.Response(
            200,
            json=completion(
                choices=[
                    {"message": {"content": "", "refusal": "I will not."}, "finish_reason": "stop"}
                ]
            ),
        )
    )
    with pytest.raises(ModelRefusedError, match="I will not"):
        await refused.provider.complete(ask())

    filtered = build(
        httpx.Response(
            200,
            json=completion(
                choices=[{"message": {"content": ""}, "finish_reason": "content_filter"}]
            ),
        )
    )
    with pytest.raises(ModelRefusedError, match="content filter"):
        await filtered.provider.complete(ask())


async def test_an_error_object_in_a_two_hundred_is_the_failure_it_describes(build) -> None:
    """Some providers answer 200 with an error body. The status is not the truth."""
    transient = build(
        httpx.Response(200, json={"error": {"code": "rate_limit_exceeded", "message": "slow"}})
    )
    with pytest.raises(ModelUnavailableError, match="rate_limit_exceeded"):
        await transient.provider.complete(ask())

    permanent = build(
        httpx.Response(200, json={"error": {"type": "invalid_request", "message": "no"}})
    )
    with pytest.raises(ModelCallFailedError, match="invalid_request"):
        await permanent.provider.complete(ask())


async def test_an_empty_choices_list_is_an_empty_reply_rather_than_a_crash(build) -> None:
    fake = build(httpx.Response(200, json=completion(choices=[])))
    reply = await fake.provider.complete(ask())
    assert reply.text == ""
    assert reply.stop is Stop.end_turn


async def test_a_wire_failure_is_the_seams_failure(build) -> None:
    fake = build(httpx.Response(503, text="down"))
    with pytest.raises(ModelUnavailableError):
        await fake.provider.complete(ask())


# --------------------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------------------


async def test_the_stream_asks_for_usage_and_assembles_the_reply_from_what_streamed(build) -> None:
    fake = build(
        httpx.Response(
            200,
            text=sse(
                chunk({"role": "assistant", "reasoning_content": "Count "}),
                chunk({"reasoning_content": "days."}),
                chunk({"content": "Tues"}),
                chunk({"content": "day."}, finish="stop"),
                {
                    "id": "x",
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 40},
                    },
                },
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    chunks = [piece async for piece in fake.provider.stream(ask())]

    assert fake.body()["stream"] is True
    assert fake.body()["stream_options"] == {"include_usage": True}
    assert [(piece.kind, piece.text) for piece in chunks[:-1]] == [
        (CHUNK_REASONING, "Count "),
        (CHUNK_REASONING, "days."),
        (CHUNK_TEXT, "Tues"),
        (CHUNK_TEXT, "day."),
    ]
    done = chunks[-1]
    assert done.kind == CHUNK_DONE
    assert done.reply is not None
    assert done.reply.text == "Tuesday."
    assert done.reply.reasoning == "Count days."
    assert done.reply.stop is Stop.end_turn
    assert done.reply.usage == Usage(input_tokens=10, output_tokens=5, cache_read_tokens=40)


async def test_a_streamed_plan_is_parsed_at_the_end_and_its_deltas_are_still_the_json(
    build,
) -> None:
    text = json.dumps(PLAN)
    fake = build(
        httpx.Response(
            200,
            text=sse(chunk({"content": text[:8]}), chunk({"content": text[8:]}, finish="stop")),
            headers={"content-type": "text/event-stream"},
        )
    )
    chunks = [piece async for piece in fake.provider.stream(ask(plan_schema=PLAN_SCHEMA))]
    assert "".join(piece.text for piece in chunks[:-1]) == text
    assert chunks[-1].reply is not None
    assert chunks[-1].reply.plan == PLAN


async def test_a_streamed_length_stop_is_resumable(build) -> None:
    fake = build(
        httpx.Response(
            200,
            text=sse(chunk({"content": "Tues"}, finish="length")),
            headers={"content-type": "text/event-stream"},
        )
    )
    chunks = [piece async for piece in fake.provider.stream(ask())]
    assert chunks[-1].reply is not None
    assert chunks[-1].reply.stop is Stop.max_tokens


async def test_an_error_object_mid_stream_is_raised_as_the_failure_it_names(build) -> None:
    fake = build(
        httpx.Response(
            200,
            text=sse(
                chunk({"content": "Tues"}),
                {"error": {"code": "server_error", "message": "hiccup"}},
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    with pytest.raises(ModelUnavailableError, match="server_error"):
        _ = [piece async for piece in fake.provider.stream(ask())]


# --------------------------------------------------------------------------------------
# The catalogue rows the adapter is built from
# --------------------------------------------------------------------------------------


def test_a_real_row_binds_with_its_own_base_url() -> None:
    spec = spec_for("deepseek")
    assert spec is not None
    binding = Binding(spec, api_key="sk-live")
    assert binding.url == "https://api.deepseek.com"
    assert Binding(spec, base_url="http://proxy.local").url == "http://proxy.local"
