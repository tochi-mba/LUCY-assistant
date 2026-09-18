"""The stream projector: spoken text waits for `done`; reasoning waits for a setting."""

from __future__ import annotations

from lucy_api.model.types import Chunk, Reply
from lucy_api.model.wire import CHUNK_DONE, CHUNK_REASONING, CHUNK_TEXT
from lucy_api.stream.events import (
    CONTENT_REASONING_DELTA,
    CONTENT_REASONING_END,
    CONTENT_REASONING_START,
    CONTENT_TEXT_DELTA,
    CONTENT_TEXT_END,
    CONTENT_TEXT_START,
)
from lucy_api.turn.project import StreamProjector


class Sink:
    def __init__(self) -> None:
        self.types: list[str] = []

    async def emit(self, session_id: str, event: object) -> None:
        del session_id
        self.types.append(getattr(event, "type", ""))


async def test_spoken_text_is_published_only_after_a_done_chunk_without_a_plan() -> None:
    sink = Sink()
    project = StreamProjector(sink, "ses_1", "trn_1")

    await project(Chunk(kind=CHUNK_TEXT, text="Hello"))
    assert sink.types == []
    await project(Chunk(kind=CHUNK_DONE, reply=Reply(text="Hello")))

    assert sink.types == [CONTENT_TEXT_START, CONTENT_TEXT_DELTA, CONTENT_TEXT_END]


async def test_a_plan_round_never_forwards_its_text_as_something_a_person_should_read() -> None:
    sink = Sink()
    project = StreamProjector(sink, "ses_1", "trn_1")

    await project(Chunk(kind=CHUNK_TEXT, text='{"steps":[]}'))
    await project(Chunk(kind=CHUNK_DONE, reply=Reply(text='{"steps":[]}', plan={"steps": []})))

    assert sink.types == []


async def test_reasoning_is_held_back_unless_stream_thinking_is_on() -> None:
    sink = Sink()
    project = StreamProjector(sink, "ses_1", "trn_1")

    await project(Chunk(kind=CHUNK_REASONING, text="hmm"))
    await project(Chunk(kind=CHUNK_DONE, reply=Reply(reasoning="hmm", text="Hi")))

    assert CONTENT_REASONING_START not in sink.types
    assert CONTENT_REASONING_DELTA not in sink.types


async def test_reasoning_is_streamed_as_it_arrives_when_the_person_asked() -> None:
    sink = Sink()
    project = StreamProjector(sink, "ses_1", "trn_1", stream_thinking=True)

    await project(Chunk(kind=CHUNK_REASONING, text="hmm"))
    await project(Chunk(kind=CHUNK_REASONING, text=" still"))
    await project(Chunk(kind=CHUNK_DONE, reply=Reply(reasoning="hmm still")))

    assert sink.types == [
        CONTENT_REASONING_START,
        CONTENT_REASONING_DELTA,
        CONTENT_REASONING_DELTA,
        CONTENT_REASONING_END,
    ]


async def test_an_unknown_chunk_kind_is_ignored() -> None:
    sink = Sink()
    project = StreamProjector(sink, "ses_1", "trn_1")

    await project(Chunk(kind="usage", text="1"))
    await project(Chunk(kind=CHUNK_TEXT, text=""))
    await project(Chunk(kind=CHUNK_REASONING, text=""))
    await project(Chunk(kind=CHUNK_DONE))

    assert sink.types == []
