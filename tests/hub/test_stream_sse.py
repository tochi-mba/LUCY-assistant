"""What goes on the wire, and what a client gets when it comes back to it.

The frames are parsed back out of the encoded text rather than asserted as string literals.
A test that compares against `"id: 4\\nevent: ...\\n"` passes for the wrong reason the day
somebody reorders two fields, and fails for the wrong reason the day somebody adds one; what
matters is that a client parsing SSE sees the right ids, in the right order, once each.

The resumption test at the end runs the real store, the real emitter and this encoder
together, because that is the path a laptop closing its lid actually takes.
"""

from __future__ import annotations

import json
import sqlite3
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.sessions.sql_store import SessionStore, identifier
from lucy_api.store.worker import SqlWorker
from lucy_api.stream import events as taxonomy
from lucy_api.stream import sse
from lucy_api.stream.emitter import EventEmitter, NewEvent, SqlEventLog, Subscriber
from lucy_api.stream.events import Event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

SESSION = "ses_sse"


class FakeSnapshotter:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id, "status": "in_progress"}


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore) -> str:
    session = identifier("ses")
    now = time.time()

    def apply(db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO sessions (id,account_id,profile,title,status,model,thinking_config,"
            "persona,harness_version,input_policy,durability_mode,permission_mode,incognito,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session,
                "acct_sse",
                "personal",
                "Streaming",
                "idle",
                "openai:gpt-5",
                "default",
                "default",
                "0.1.0",
                "enqueue",
                "durable",
                "ask",
                0,
                now,
                now,
            ),
        )

    await store.transaction(apply)
    return session


def an_event(sequence: int, kind: str = taxonomy.CONTENT_TEXT_DELTA, **data: Any) -> Event:
    return Event(
        type=kind,
        sequence_number=sequence,
        event_id=f"evt_{sequence}",
        session_id=SESSION,
        created_at=float(sequence),
        data=data,
    )


def parse(text: str) -> list[dict[str, str]]:
    """The frames a client would see, as field dictionaries."""
    parsed: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                fields["comment"] = line[1:].strip()
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        parsed.append(fields)
    return parsed


async def take(stream: AsyncIterator[str], count: int) -> list[dict[str, str]]:
    """The first `count` frames, then let go of the generator."""
    collected: list[str] = []
    try:
        async for chunk in stream:
            collected.append(chunk)
            if len(collected) == count:
                break
    finally:
        await stream.aclose()  # type: ignore[attr-defined]
    return parse("".join(collected))


def test_the_query_cursor_wins_over_the_header_a_browser_replayed() -> None:
    """One was chosen by the client; the other survived however many proxies."""
    assert sse.resume_from("41", "7") == 41
    assert sse.resume_from(None, "7") == 7
    assert sse.resume_from("0", None) == 0
    assert sse.resume_from(None, None) is None


def test_a_cursor_that_cannot_be_understood_is_refused_in_words_that_name_the_fix() -> None:
    """Treating it as a fresh connection would drop the deltas without saying so."""
    for supplied, replayed, expected in (
        ("nonsense", None, "starting_after"),
        (None, "evt_9RnQ", "Last-Event-ID"),
        ("-1", None, "starting_after"),
        (None, "-4", "Last-Event-ID"),
    ):
        with pytest.raises(LucyError) as caught:
            sse.resume_from(supplied, replayed)
        assert caught.value.code == sse.CURSOR_PROBLEM
        assert caught.value.status == HTTPStatus.BAD_REQUEST
        assert expected in str(caught.value)
        assert "sequence_number" in str(caught.value) or "negative" in str(caught.value)


def test_a_frame_is_identified_by_its_sequence_number_and_carries_the_event_id_inside() -> None:
    """`Last-Event-ID` is only useful if it can be ordered, so the id on the wire is the one
    that can be."""
    frame = parse(sse.encode(an_event(4, taxonomy.TOOL_STARTED, operation="music.play")))[0]

    assert frame["id"] == "4"
    assert frame["event"] == taxonomy.TOOL_STARTED
    body = json.loads(frame["data"])
    assert body["event_id"] == "evt_4"
    assert body["sequence_number"] == 4
    assert body["data"] == {"operation": "music.play"}
    assert "turn_id" not in body


def test_a_payload_full_of_newlines_still_arrives_as_one_frame() -> None:
    """JSON escapes the only character that could have split the body, so nothing else has
    to re-join it."""
    encoded = sse.encode(an_event(5, taxonomy.TOOL_FINISHED, output="one\ntwo\n\nthree"))

    assert encoded.count("data: ") == 1
    assert encoded.endswith("\n\n")
    assert len(encoded.rstrip("\n").split("\n")) == 3
    assert json.loads(parse(encoded)[0]["data"])["data"]["output"] == "one\ntwo\n\nthree"


def test_a_heartbeat_carries_no_id_so_it_cannot_move_a_browsers_cursor() -> None:
    frame = parse(sse.heartbeat(SESSION))[0]

    assert "id" not in frame
    assert frame["event"] == taxonomy.STREAM_HEARTBEAT
    assert json.loads(frame["data"])["session_id"] == SESSION


def test_the_response_headers_keep_proxies_from_buffering_the_stream() -> None:
    assert sse.HEADERS["Content-Type"].startswith(sse.MEDIA_TYPE)
    assert "no-transform" in sse.HEADERS["Cache-Control"]
    assert sse.HEADERS["X-Accel-Buffering"] == "no"
    # Meaningless in HTTP/1.1 and forbidden in HTTP/2; sending it is either noise or a bug.
    assert "Connection" not in sse.HEADERS


async def test_a_stream_that_ends_says_so_and_says_where_to_resume_from() -> None:
    subscriber = Subscriber(SESSION, capacity=4)
    subscriber.deliver(an_event(1))
    subscriber.close()

    frames = [frame async for frame in sse.frames(subscriber)]
    parsed = parse("".join(frames))

    assert parsed[0]["retry"] == str(sse.RETRY_MILLISECONDS)
    assert parsed[1]["id"] == "1"
    assert parsed[-1]["event"] == taxonomy.STREAM_DONE
    assert json.loads(parsed[-1]["data"])["data"] == {"starting_after": 1}


async def test_a_reader_that_fell_behind_is_told_so_in_the_words_that_fix_it() -> None:
    """A connection that merely stops is indistinguishable from a network failure."""
    subscriber = Subscriber(SESSION, capacity=1)
    subscriber.deliver(an_event(1))
    subscriber.deliver(an_event(2))

    parsed = parse("".join([frame async for frame in sse.frames(subscriber)]))

    assert [frame.get("id") for frame in parsed[1:-1]] == ["1"]
    assert parsed[-1]["event"] == taxonomy.STREAM_ERROR
    body = json.loads(parsed[-1]["data"])["data"]
    assert body["reason"] == sse.SLOW_CONSUMER
    assert body["starting_after"] == 1
    assert "?starting_after=1" in body["message"]


async def test_a_quiet_stream_keeps_writing_heartbeats_rather_than_ending() -> None:
    """A dropped connection is not a cancellation, so silence must not close the stream."""
    subscriber = Subscriber(SESSION, capacity=4)

    parsed = await take(sse.frames(subscriber, heartbeat_seconds=0.01), 3)

    assert parsed[0]["retry"] == str(sse.RETRY_MILLISECONDS)
    assert [frame["event"] for frame in parsed[1:]] == [taxonomy.STREAM_HEARTBEAT] * 2
    assert not subscriber.ended


async def test_a_client_that_loses_the_wire_comes_back_to_exactly_what_it_missed(
    store: SessionStore,
) -> None:
    """The whole design, end to end: emit, read, disconnect, emit, resume, compare.

    The two assertions that matter are that the second connection's event frames are 4, 5
    and 6 -- nothing missed -- and that 1, 2 and 3 appear on the first connection and never
    again.
    """
    session = await a_session(store)
    emitter = EventEmitter(SqlEventLog(store), FakeSnapshotter())

    async with emitter.subscribe(session) as first:
        for index in range(3):
            await emitter.emit(session, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))
        before = await take(sse.frames(first, heartbeat_seconds=5), 5)

    assert [frame["event"] for frame in before[1:2]] == [taxonomy.STREAM_SNAPSHOT]
    assert [frame["id"] for frame in before[2:]] == ["1", "2", "3"]

    for index in range(3, 6):
        await emitter.emit(session, NewEvent(taxonomy.CONTENT_TEXT_DELTA, {"n": index}))

    resume = sse.resume_from(before[-1]["id"], None)
    async with emitter.subscribe(session, starting_after=resume) as second:
        after = await take(sse.frames(second, heartbeat_seconds=5), 6)

    assert [frame["event"] for frame in after[1:3]] == [
        taxonomy.STREAM_SNAPSHOT,
        taxonomy.STREAM_RESUMED,
    ]
    # The two transport frames claim the client's own position, so a client that died on
    # the snapshot would ask for the same thing again rather than skipping ahead.
    assert [frame["id"] for frame in after[1:3]] == ["3", "3"]
    assert [frame["id"] for frame in after[3:]] == ["4", "5", "6"]
    assert [json.loads(frame["data"])["data"]["n"] for frame in after[3:]] == [3, 4, 5]
