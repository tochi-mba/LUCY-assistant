"""The negotiated encoding, tested as a projection rather than as a translation.

A translation would be asserted event for event. This is not one: most of the catalogue has
no counterpart in the AI SDK's vocabulary and is deliberately dropped, so the tests that
matter are the ones pinning *which* events survive, what they turn into, and that everything
else leaves no trace. The alternative -- inventing a `data-*` part for each of a hundred and
sixty-nine event types -- would be forking the protocol while claiming to speak it, and the
dropping test below is what stops somebody doing it by accident.
"""

from __future__ import annotations

import json
from typing import Any

from lucy_api.stream import ai_sdk
from lucy_api.stream import events as taxonomy
from lucy_api.stream.emitter import Subscriber
from lucy_api.stream.events import Event

SESSION = "ses_ui"
TURN = "trn_ui"


def an_event(kind: str, *, turn: str | None = TURN, sequence: int = 1, **data: Any) -> Event:
    return Event(
        type=kind,
        sequence_number=sequence,
        event_id=f"evt_{sequence}",
        session_id=SESSION,
        created_at=1.0,
        data=data,
        turn_id=turn,
    )


def parts_of(kind: str, *, turn: str | None = TURN, **data: Any) -> list[dict[str, Any]]:
    return list(ai_sdk.parts(an_event(kind, turn=turn, **data)))


def bodies(chunks: list[str]) -> list[Any]:
    """Each frame's payload, with the sentinel left as the string it is."""
    decoded: list[Any] = []
    for chunk in chunks:
        payload = chunk.removeprefix("data: ").removesuffix("\n\n")
        decoded.append(payload if payload == "[DONE]" else json.loads(payload))
    return decoded


async def collect(subscriber: Subscriber, *, heartbeat_seconds: float = 5.0) -> list[str]:
    return [chunk async for chunk in ai_sdk.frames(subscriber, heartbeat_seconds=heartbeat_seconds)]


def test_the_encoding_is_chosen_only_when_both_signals_arrive() -> None:
    """A header alone would hand the protocol to a client that never asked for a stream."""
    assert ai_sdk.negotiated("text/event-stream", "v1")
    assert ai_sdk.negotiated("text/event-stream, */*;q=0.1", " V1 ")

    assert not ai_sdk.negotiated("text/event-stream", None)
    assert not ai_sdk.negotiated("text/event-stream", "v2")
    assert not ai_sdk.negotiated("application/json", "v1")
    assert not ai_sdk.negotiated(None, "v1")


def test_the_response_headers_carry_the_name_the_sdk_reads_and_the_name_the_client_sent() -> None:
    assert ai_sdk.RESPONSE_HEADERS[ai_sdk.VERCEL_HEADER] == ai_sdk.PROTOCOL_VERSION
    assert ai_sdk.RESPONSE_HEADERS[ai_sdk.NEGOTIATION_HEADER] == ai_sdk.PROTOCOL_VERSION
    assert ai_sdk.RESPONSE_HEADERS["Content-Type"].startswith("text/event-stream")


def test_a_turn_becomes_a_message_and_keeps_the_id_the_rest_of_the_api_uses() -> None:
    assert parts_of(taxonomy.TURN_STARTED) == [{"type": "start", "messageId": TURN}]
    assert parts_of(taxonomy.TURN_STARTED, turn=None) == [{"type": "start"}]


def test_text_and_reasoning_arrive_as_the_sdks_own_three_part_shape() -> None:
    assert parts_of(taxonomy.CONTENT_TEXT_START, id="blk") == [{"type": "text-start", "id": "blk"}]
    assert parts_of(taxonomy.CONTENT_TEXT_DELTA, id="blk", delta="hel") == [
        {"type": "text-delta", "id": "blk", "delta": "hel"}
    ]
    assert parts_of(taxonomy.CONTENT_TEXT_END, id="blk") == [{"type": "text-end", "id": "blk"}]
    assert parts_of(taxonomy.CONTENT_REASONING_START, id="r") == [
        {"type": "reasoning-start", "id": "r"}
    ]
    assert parts_of(taxonomy.CONTENT_REASONING_DELTA, id="r", delta="because") == [
        {"type": "reasoning-delta", "id": "r", "delta": "because"}
    ]
    assert parts_of(taxonomy.CONTENT_REASONING_END, id="r") == [
        {"type": "reasoning-end", "id": "r"}
    ]


def test_a_block_with_no_id_of_its_own_borrows_the_turns_and_then_the_sessions() -> None:
    """Every delta of one block has to carry the same id or the client renders them apart."""
    assert parts_of(taxonomy.CONTENT_TEXT_DELTA, delta="a")[0]["id"] == TURN
    assert parts_of(taxonomy.CONTENT_TEXT_DELTA, turn=None, delta="a")[0]["id"] == SESSION


def test_a_tool_call_fills_in_its_arguments_and_then_hands_back_its_result() -> None:
    assert parts_of(taxonomy.TOOL_INPUT_START, tool_call_id="call_1", tool_name="music.play") == [
        {"type": "tool-input-start", "toolCallId": "call_1", "toolName": "music.play"}
    ]
    assert parts_of(taxonomy.TOOL_INPUT_DELTA, tool_call_id="call_1", delta='{"q"') == [
        {"type": "tool-input-delta", "toolCallId": "call_1", "inputTextDelta": '{"q"'}
    ]
    assert parts_of(
        taxonomy.TOOL_INPUT_AVAILABLE,
        tool_call_id="call_1",
        tool_name="music.play",
        input={"q": "hounds"},
    ) == [
        {
            "type": "tool-input-available",
            "toolCallId": "call_1",
            "toolName": "music.play",
            "input": {"q": "hounds"},
        }
    ]
    assert parts_of(taxonomy.TOOL_FINISHED, tool_call_id="call_1", output={"ref": "$hits"}) == [
        {"type": "tool-output-available", "toolCallId": "call_1", "output": {"ref": "$hits"}}
    ]


def test_a_failed_tool_says_so_rather_than_leaving_a_spinner_turning() -> None:
    assert parts_of(taxonomy.TOOL_FAILED, tool_call_id="call_1", message="the service is down") == [
        {"type": "tool-output-error", "toolCallId": "call_1", "errorText": "the service is down"}
    ]


def test_a_tool_event_naming_no_call_is_dropped_rather_than_attached_to_the_wrong_one() -> None:
    for kind in (
        taxonomy.TOOL_INPUT_START,
        taxonomy.TOOL_INPUT_DELTA,
        taxonomy.TOOL_INPUT_AVAILABLE,
        taxonomy.TOOL_FINISHED,
        taxonomy.TOOL_FAILED,
    ):
        assert parts_of(kind) == []


def test_an_approval_request_crosses_with_the_call_it_is_about_when_it_names_one() -> None:
    assert parts_of(taxonomy.APPROVAL_REQUESTED, approval_id="apr_1", tool_call_id="call_1") == [
        {"type": "tool-approval-request", "approvalId": "apr_1", "toolCallId": "call_1"}
    ]
    assert parts_of(taxonomy.APPROVAL_REQUESTED) == [
        {"type": "tool-approval-request", "approvalId": "evt_1"}
    ]


def test_a_missing_connection_crosses_as_a_data_part_keyed_so_it_can_be_replaced() -> None:
    """Asking twice about Spotify should redraw one prompt, not stack a second under it."""
    assert parts_of(taxonomy.CONNECTION_REQUIRED, service="music", connect_url="https://x") == [
        {
            "type": ai_sdk.CONNECTION_REQUIRED_PART,
            "id": "music",
            "data": {"service": "music", "connect_url": "https://x"},
        }
    ]
    assert parts_of(taxonomy.CONNECTION_REQUIRED)[0]["id"] == "evt_1"


def test_a_turn_that_failed_reports_the_error_before_it_reports_the_finish() -> None:
    assert parts_of(taxonomy.TURN_COMPLETED) == [{"type": "finish"}]
    assert parts_of(taxonomy.TURN_CANCELLED) == [{"type": "finish"}]
    assert parts_of(taxonomy.TURN_FAILED, message="the model refused") == [
        {"type": "error", "errorText": "the model refused"},
        {"type": "finish"},
    ]
    assert parts_of(taxonomy.STREAM_ERROR, message="fell behind") == [
        {"type": "error", "errorText": "fell behind"}
    ]


def test_an_event_with_no_counterpart_leaves_no_trace_in_this_encoding() -> None:
    """The catalogue is ten times this protocol's size; the extra is dropped, not invented."""
    for kind in (
        taxonomy.MEMORY_WRITTEN,
        taxonomy.CAPABILITY_PROBE_FAILED,
        taxonomy.TASK_LEASE_EXPIRED,
        taxonomy.STREAM_SNAPSHOT,
        "lucy.acme.widget.polished",
    ):
        assert parts_of(kind) == []

    assert set(ai_sdk.CONVERTERS) < taxonomy.EVENT_TYPES | {taxonomy.STREAM_ERROR}


def test_one_part_is_one_data_frame() -> None:
    encoded = ai_sdk.encode({"type": "text-delta", "id": "blk", "delta": "hi"})

    assert encoded.startswith("data: ")
    assert encoded.endswith("\n\n")
    assert json.loads(encoded[6:-2]) == {"type": "text-delta", "id": "blk", "delta": "hi"}


async def test_a_finished_turn_ends_with_the_sentinel_the_sdk_stops_on() -> None:
    subscriber = Subscriber(SESSION, capacity=8)
    subscriber.deliver(an_event(taxonomy.TURN_STARTED))
    subscriber.deliver(an_event(taxonomy.CONTENT_TEXT_DELTA, sequence=2, id="blk", delta="hello"))
    subscriber.deliver(an_event(taxonomy.MEMORY_WRITTEN, sequence=3, topic="home"))
    subscriber.deliver(an_event(taxonomy.TURN_COMPLETED, sequence=4))
    subscriber.close()

    assert bodies(await collect(subscriber)) == [
        {"type": "start", "messageId": TURN},
        {"type": "text-delta", "id": "blk", "delta": "hello"},
        {"type": "finish"},
        "[DONE]",
    ]


async def test_a_reader_that_fell_behind_gets_an_error_part_and_then_the_sentinel() -> None:
    subscriber = Subscriber(SESSION, capacity=1)
    subscriber.deliver(an_event(taxonomy.TURN_STARTED))
    subscriber.deliver(an_event(taxonomy.TURN_COMPLETED))

    decoded = bodies(await collect(subscriber))

    assert decoded[0] == {"type": "start", "messageId": TURN}
    assert decoded[-2]["type"] == "error"
    assert "?starting_after=" in decoded[-2]["errorText"]
    assert decoded[-1] == "[DONE]"


async def test_a_quiet_stream_pings_with_a_comment_the_sdks_parser_never_sees() -> None:
    """A part type this reader's version has never heard of is a risk taken for nothing."""
    subscriber = Subscriber(SESSION, capacity=4)
    collected: list[str] = []
    stream = ai_sdk.frames(subscriber, heartbeat_seconds=0.01)
    try:
        async for chunk in stream:
            collected.append(chunk)
            if len(collected) == 2:
                break
    finally:
        await stream.aclose()

    assert collected == [ai_sdk.PING, ai_sdk.PING]
    assert all(chunk.startswith(":") for chunk in collected)
