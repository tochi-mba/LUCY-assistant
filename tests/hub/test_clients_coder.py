"""The coder client: the bridge's answers carried across with nothing invented."""

from __future__ import annotations

import pytest

from lucy_api.clients.coder import AUDIENCE, CoderTask, FakeCoderClient, HttpCoderClient
from lucy_api.clients.testing import Answer, FakeHttp

ROW = {
    "id": "tsk_1",
    "title": "version flag",
    "brief": "add --version",
    "directory": "C:/code/tool",
    "run_level": "edits",
    "state": "idle",
    "detail": "the turn is over",
    "result": "done",
    "turns": 2,
    "cost_usd": 0.0421,
    "tool_uses": 7,
    "last_tool": "Edit",
    "last_text": "all green",
    "queued_messages": 1,
    "resumable": True,
}


async def test_every_route_speaks_to_the_coder_audience_with_the_right_shape() -> None:
    http = FakeHttp(
        Answer(body={"status": "ok"}),
        Answer(status_code=201, body=ROW),
        Answer(body={**ROW, "transcript_tail": "{...}"}),
        Answer(body={"tasks": [ROW, ROW]}),
        Answer(body={**ROW, "advice": "runs when this turn ends"}),
        Answer(body={**ROW, "state": "cancelled"}),
    )
    client = HttpCoderClient(http, "http://coder.test/")

    assert (await client.ready()).ready is True
    started = await client.start(
        brief="add --version", directory="C:/code/tool", run_level="edits", title="t"
    )
    read = await client.get("tsk_1", tail_chars=4000)
    listed = await client.tasks()
    said = await client.message("tsk_1", "and a test")
    stopped = await client.cancel("tsk_1")

    assert all(call.audience == AUDIENCE == "coder-api" for call in http.calls)
    paths = [(call.method, call.url) for call in http.calls]
    assert paths == [
        ("GET", "http://coder.test/healthy"),
        ("POST", "http://coder.test/v1/tasks"),
        ("GET", "http://coder.test/v1/tasks/tsk_1"),
        ("GET", "http://coder.test/v1/tasks"),
        ("POST", "http://coder.test/v1/tasks/tsk_1/message"),
        ("POST", "http://coder.test/v1/tasks/tsk_1/cancel"),
    ]
    assert http.calls[1].json == {
        "brief": "add --version",
        "directory": "C:/code/tool",
        "run_level": "edits",
        "title": "t",
    }
    assert http.calls[2].params == {"tail_chars": 4000}
    assert http.calls[4].json == {"text": "and a test"}
    assert started.cost_usd == pytest.approx(0.0421), "cents survive the trip"
    assert started.turns == 2
    assert started.resumable is True
    assert started.live is False
    assert read.transcript_tail == "{...}"
    assert len(listed) == 2
    assert said.advice == "runs when this turn ends"
    assert stopped.state == "cancelled"


async def test_a_read_without_a_tail_asks_for_none_and_a_task_id_is_one_path_segment() -> None:
    http = FakeHttp(Answer(body=ROW))
    client = HttpCoderClient(http, "http://coder.test")
    await client.get("tsk/../other")
    assert http.calls[0].params is None
    assert http.calls[0].url == "http://coder.test/v1/tasks/tsk%2F..%2Fother"


async def test_an_unhealthy_answer_and_a_garbled_cost_read_honestly() -> None:
    http = FakeHttp(
        Answer(body={"status": "degraded"}),
        Answer(body={**ROW, "cost_usd": "lots", "state": "running"}),
    )
    client = HttpCoderClient(http, "http://coder.test")
    assert (await client.ready()).ready is False
    task = await client.get("tsk_1")
    assert task.cost_usd == 0.0
    assert task.live is True


async def test_the_fake_records_and_cancels_like_the_bridge() -> None:
    fake = FakeCoderClient()
    made = await fake.start(brief="b" * 80, directory="C:/x", run_level="plan", title="")
    assert made.title == "b" * 60, "an unnamed task is named by its brief"
    assert made.live
    assert [task.id for task in await fake.tasks()] == [made.id]
    assert await fake.get(made.id, tail_chars=10) == made
    assert await fake.message(made.id, "hi") == made
    ended = await fake.cancel(made.id)
    assert isinstance(ended, CoderTask)
    assert ended.state == "cancelled"
    assert fake.cancelled == [made.id]

    fake.down = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await fake.start(brief="b", directory="C:/x", run_level="plan", title="")
    with pytest.raises(RuntimeError):
        await fake.ready()


async def test_mode_tools_and_model_cross_only_when_given() -> None:
    http = FakeHttp(
        Answer(status_code=201, body=ROW),
        Answer(status_code=201, body=ROW),
        Answer(body=ROW),
        Answer(body={**ROW, "permission_denials": [{"tool": "Write", "input": "{}"}, "junk"]}),
    )
    client = HttpCoderClient(http, "http://coder.test")
    await client.start(brief="b", directory="C:/x", run_level="plan", title="", model="sonnet")
    await client.start(brief="b", directory="C:/x", run_level="plan", title="")
    await client.message("tsk_1", "go")
    denied = await client.message("tsk_1", "go", mode="ask", allow_tools=("Write",))
    assert http.calls[0].json["model"] == "sonnet"
    assert "model" not in http.calls[1].json
    assert http.calls[2].json == {"text": "go"}
    assert http.calls[3].json == {"text": "go", "mode": "ask", "allow_tools": ["Write"]}
    assert denied.permission_denials == ({"tool": "Write", "input": "{}"},)
