"""The fake the whole agent loop will be tested with, tested itself.

A golden transcript is only worth as much as the model that drives it. If the fake can
repeat itself, a loop that never terminates passes; if it cannot express a refusal, the
one path a client must never offer to retry goes unasserted. So these tests are mostly
about the awkward half: running off the end, the three kinds of failure, and the record
of what the model was actually asked.
"""

from __future__ import annotations

from typing import Any

import pytest

from lucy_api.model.scripted import (
    MALFORMED_PLAN,
    ScriptedProvider,
    ScriptExhaustedError,
    Step,
    fails,
    flakes,
    malformed,
    plans,
    raises,
    refuses,
    runs_out_of_room,
    speaks,
)
from lucy_api.model.types import (
    Message,
    ModelRefusedError,
    ModelUnavailableError,
    Reply,
    Request,
    Role,
    Stop,
    Usage,
)
from lucy_api.model.wire import CHUNK_DONE, CHUNK_REASONING, CHUNK_TEXT, ModelCallFailedError

PLAN: dict[str, Any] = {"steps": [{"operation": "notes.search", "arguments": {"q": "tax"}}]}


def ask(**overrides: Any) -> Request:
    defaults: dict[str, Any] = {"messages": [Message(role=Role.user, content="hello")]}
    return Request(**{**defaults, **overrides})


async def test_the_script_is_served_in_order_one_reply_per_call() -> None:
    provider = ScriptedProvider([speaks("first"), speaks("second")])
    assert (await provider.complete(ask())).text == "first"
    assert (await provider.complete(ask())).text == "second"
    assert provider.calls == 2
    assert provider.remaining == 0


async def test_running_off_the_end_of_the_script_fails_loudly_and_counts_both_sides() -> None:
    provider = ScriptedProvider([speaks("only one")])
    await provider.complete(ask())
    with pytest.raises(ScriptExhaustedError, match="reply 2 but the script has 1"):
        await provider.complete(ask())


async def test_the_request_that_ran_off_the_end_is_recorded_because_it_is_the_evidence() -> None:
    provider = ScriptedProvider()
    with pytest.raises(ScriptExhaustedError):
        await provider.complete(ask(system="you are lucy"))
    assert [request.system for request in provider.requests] == ["you are lucy"]


async def test_every_request_is_kept_so_a_test_can_assert_the_exact_assembled_prompt() -> None:
    provider = ScriptedProvider([speaks("ok"), speaks("ok")])
    first = ask(system="rules", messages=[Message(role=Role.user, content="hi")])
    second = ask(
        system="rules",
        messages=[
            Message(role=Role.user, content="hi"),
            Message(role=Role.assistant, content="ok"),
            Message(role=Role.user, content="again"),
        ],
        plan_schema={"type": "object"},
    )
    await provider.complete(first)
    await provider.complete(second)
    assert provider.requests == [first, second]
    assert provider.requests[1].messages[-1].content == "again"


async def test_a_plain_reply_carries_no_plan_and_ends_the_turn() -> None:
    provider = ScriptedProvider([speaks("hello there", reasoning="brief", model="scripted-1")])
    reply = await provider.complete(ask())
    assert reply == Reply(
        text="hello there",
        plan=None,
        reasoning="brief",
        stop=Stop.end_turn,
        usage=Usage(),
        model="scripted-1",
    )


async def test_a_reply_carrying_a_plan_stops_for_tool_use_because_the_turn_is_not_over() -> None:
    provider = ScriptedProvider([plans(PLAN, usage=Usage(input_tokens=30, output_tokens=9))])
    reply = await provider.complete(ask())
    assert reply.plan == PLAN
    assert reply.stop is Stop.tool_use
    assert reply.usage == Usage(input_tokens=30, output_tokens=9)


async def test_a_malformed_plan_is_valid_json_in_a_shape_no_schema_accepts() -> None:
    provider = ScriptedProvider([malformed()])
    reply = await provider.complete(ask())
    assert reply.plan == MALFORMED_PLAN
    assert reply.plan is not MALFORMED_PLAN, "a test must not be able to edit the constant"


async def test_a_malformed_plan_can_be_spelled_out_when_a_test_needs_a_specific_one() -> None:
    provider = ScriptedProvider([malformed({"steps": [{"operation": 7}]}, text="here you go")])
    reply = await provider.complete(ask())
    assert reply.plan == {"steps": [{"operation": 7}]}
    assert reply.text == "here you go"


async def test_output_that_is_not_json_at_all_is_scripted_as_plain_text() -> None:
    provider = ScriptedProvider([speaks('{"steps": [')])
    reply = await provider.complete(ask())
    assert reply.plan is None
    assert reply.text == '{"steps": ['


async def test_a_reply_cut_short_stops_for_max_tokens_and_keeps_what_it_managed_to_say() -> None:
    provider = ScriptedProvider(
        [runs_out_of_room("I was saying that", usage=Usage(output_tokens=4))]
    )
    reply = await provider.complete(ask())
    assert reply.stop is Stop.max_tokens
    assert reply.text == "I was saying that"
    assert reply.usage.output_tokens == 4


async def test_a_refusal_raises_the_way_a_real_provider_refuses() -> None:
    provider = ScriptedProvider([refuses("I will not help with that")])
    with pytest.raises(ModelRefusedError, match="I will not help with that"):
        await provider.complete(ask())


async def test_a_transient_failure_carries_the_interval_it_asked_to_be_retried_after() -> None:
    provider = ScriptedProvider([flakes("overloaded", retry_after=2.5)])
    with pytest.raises(ModelUnavailableError) as raised:
        await provider.complete(ask())
    assert raised.value.retry_after == 2.5


async def test_a_transient_failure_without_an_interval_leaves_the_backoff_to_the_caller() -> None:
    provider = ScriptedProvider([flakes("overloaded")])
    with pytest.raises(ModelUnavailableError) as raised:
        await provider.complete(ask())
    assert raised.value.retry_after is None


async def test_a_permanent_failure_is_neither_retryable_nor_a_refusal() -> None:
    provider = ScriptedProvider([fails("the api key was revoked")])
    with pytest.raises(ModelCallFailedError, match="revoked"):
        await provider.complete(ask())


async def test_a_failure_of_any_other_kind_can_be_scripted_directly() -> None:
    provider = ScriptedProvider([raises(TimeoutError("the socket gave up"))])
    with pytest.raises(TimeoutError, match="the socket gave up"):
        await provider.complete(ask())


async def test_a_failing_step_still_consumes_its_turn_so_a_retry_reaches_the_next_one() -> None:
    provider = ScriptedProvider([flakes("overloaded"), speaks("second time lucky")])
    with pytest.raises(ModelUnavailableError):
        await provider.complete(ask())
    assert (await provider.complete(ask())).text == "second time lucky"


async def test_a_streamed_reply_arrives_in_pieces_and_ends_with_the_whole_thing() -> None:
    provider = ScriptedProvider([speaks("abcdefghij", reasoning="hmm")], chunk_size=4)
    chunks = [chunk async for chunk in provider.stream(ask())]
    assert [(chunk.kind, chunk.text) for chunk in chunks[:-1]] == [
        (CHUNK_REASONING, "hmm"),
        (CHUNK_TEXT, "abcd"),
        (CHUNK_TEXT, "efgh"),
        (CHUNK_TEXT, "ij"),
    ]
    assert chunks[-1].kind == CHUNK_DONE
    assert chunks[-1].reply is not None
    assert chunks[-1].reply.text == "abcdefghij"


async def test_a_streamed_reply_with_nothing_to_say_is_just_the_final_chunk() -> None:
    provider = ScriptedProvider([plans(PLAN)])
    chunks = [chunk async for chunk in provider.stream(ask())]
    assert [chunk.kind for chunk in chunks] == [CHUNK_DONE]
    assert chunks[0].reply is not None
    assert chunks[0].reply.plan == PLAN


async def test_a_streamed_failure_raises_before_any_chunk_arrives() -> None:
    provider = ScriptedProvider([refuses("no")])
    with pytest.raises(ModelRefusedError, match="no"):
        [chunk async for chunk in provider.stream(ask())]
    assert provider.calls == 1, "a refused call still happened"


def test_a_chunk_size_of_zero_is_refused_rather_than_looping_forever() -> None:
    with pytest.raises(ValueError, match="at least 1 character"):
        ScriptedProvider([speaks("hi")], chunk_size=0)


def test_an_empty_script_is_legal_and_reports_that_it_has_nothing() -> None:
    provider = ScriptedProvider()
    assert provider.remaining == 0
    assert provider.calls == 0
    assert provider.model == "scripted"


def test_a_step_hands_back_its_reply_when_it_is_not_a_failure() -> None:
    assert Step(reply=Reply(text="x")).serve().text == "x"
