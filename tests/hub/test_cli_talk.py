"""`lucy talk` is the conversation client: one write path, then the event stream.

A person types words; a script pipes them. Either way the hub's `POST /inputs` is the
only write, and closing the CLI does not cancel the turn. These tests pin that contract
against an in-memory hub, so they never wait on a real model.
"""

from __future__ import annotations

import io
import json
import os

import httpx
import pytest

from lucy_api.cli.base import OK, REFUSED, TOKEN_VAR, UNREACHABLE, USAGE, CliError
from lucy_api.cli.main import main as cli_main

REAL_CLIENT = httpx.Client


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def hub(handler):
    return REAL_CLIENT(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def isolated_cli_environment(tmp_path, monkeypatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LUCY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.chdir(tmp_path)


def run(argv, *, environ=None, stdin="", tty=False):
    out, err = FakeStream(tty=tty), FakeStream(tty=tty)
    env = {"LUCY_CONFIG": os.environ["LUCY_CONFIG"], **(environ or {})}
    code = cli_main(argv, out=out, err=err, in_=io.StringIO(stdin), environ=env)
    return code, out.getvalue(), err.getvalue()


def _sse(*frames: tuple[str, dict[str, object]]) -> str:
    parts = ["retry: 3000\n: lucy\n\n"]
    for index, (name, body) in enumerate(frames, start=1):
        payload = {
            "type": name,
            "sequence_number": index,
            "event_id": f"evt_{index}",
            "session_id": "ses_1",
            "created_at": 0,
            "data": body,
            "turn_id": "trn_1",
        }
        parts.append(f"id: {index}\nevent: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(parts)


def talking_hub(*, session_id: str = "ses_1", turn_id: str = "trn_1") -> object:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/v1/sessions":
            return httpx.Response(201, json={"id": session_id, "title": "New conversation"})
        if request.method == "POST" and path == f"/v1/sessions/{session_id}/inputs":
            return httpx.Response(202, json={"id": turn_id, "status": "queued"})
        if request.method == "GET" and path == f"/v1/sessions/{session_id}/events":
            return httpx.Response(
                200,
                text=_sse(
                    ("lucy.content.text.delta", {"delta": "Hello"}),
                    ("lucy.content.text.delta", {"delta": " there."}),
                    ("lucy.turn.completed", {"status": "completed"}),
                ),
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(f"{request.method} {path}")

    handler.seen = seen  # type: ignore[attr-defined]
    return handler


@pytest.fixture
def patched(monkeypatch):
    def install(handler):
        monkeypatch.setattr(httpx, "Client", lambda **_: hub(handler))

    return install


def test_talk_sends_the_words_and_prints_the_reply(patched) -> None:
    handler = talking_hub()
    patched(handler)
    code, out, err = run(["talk", "Hello", "Lucy"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert out == "Hello there.\n"
    posted = next(req for req in handler.seen if req.url.path.endswith("/inputs"))
    assert json.loads(posted.content) == {
        "events": [{"type": "input.message", "content": "Hello Lucy"}]
    }
    assert posted.headers["idempotency-key"]
    assert "ses_1" in err


def test_talk_reuses_a_named_session_instead_of_creating_one(patched) -> None:
    handler = talking_hub(session_id="ses_kept")
    patched(handler)
    code, out, _ = run(
        ["talk", "--session", "ses_kept", "again"],
        environ={TOKEN_VAR: "t"},
    )
    assert code == OK
    assert out == "Hello there.\n"
    assert all(req.url.path != "/v1/sessions" or req.method != "POST" for req in handler.seen)
    assert any(req.url.path == "/v1/sessions/ses_kept/inputs" for req in handler.seen)


def test_talk_reads_stdin_when_there_are_no_words(patched) -> None:
    patched(talking_hub())
    code, out, _ = run(["talk"], environ={TOKEN_VAR: "t"}, stdin="piped question\n")
    assert code == OK
    assert "Hello there." in out


def test_talk_json_names_the_session_turn_and_text(patched) -> None:
    patched(talking_hub())
    code, out, err = run(["talk", "--json", "Hi"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert err == ""
    assert json.loads(out) == {
        "session_id": "ses_1",
        "turn_id": "trn_1",
        "text": "Hello there.",
    }


def test_talk_without_a_token_names_setup(patched) -> None:
    patched(talking_hub())
    code, out, err = run(["talk", "hi"])
    assert code == REFUSED
    assert out == ""
    assert "not signed in" in err
    assert "lucy setup" in err


def test_talk_without_a_message_is_a_usage_error(patched) -> None:
    patched(talking_hub())
    code, _, err = run(["talk"], environ={TOKEN_VAR: "t"}, stdin="")
    assert code == USAGE
    assert "say something" in err


def test_a_failed_turn_is_a_refusal(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "ses_1"})
        if request.url.path.endswith("/inputs"):
            return httpx.Response(202, json={"id": "trn_1", "status": "queued"})
        return httpx.Response(
            200,
            text=_sse(("lucy.turn.failed", {"status": "failed"})),
            headers={"content-type": "text/event-stream"},
        )

    patched(handler)
    code, out, err = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert out == ""
    assert "could not finish" in err


def test_talk_cannot_reach_the_hub(monkeypatch) -> None:
    class Boom:
        def __enter__(self) -> Boom:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("nope")

        def stream(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("nope")

        def close(self) -> None:
            return None

    monkeypatch.setattr(httpx, "Client", lambda **_: Boom())
    code, _, err = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == UNREACHABLE
    assert "cannot reach Lucy" in err


def test_a_refused_message_names_the_problem(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "ses_1"})
        return httpx.Response(409, json={"detail": "owning workflow"})

    patched(handler)
    code, _, err = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert "owning workflow" in err


def test_an_interactive_prompt_sends_each_non_empty_line(patched) -> None:
    handler = talking_hub()
    patched(handler)
    in_ = FakeStream(tty=True)
    in_.write(" \none\n\ntwo\n")
    in_.seek(0)
    out, err = FakeStream(tty=True), FakeStream(tty=True)
    env = {"LUCY_CONFIG": os.environ["LUCY_CONFIG"], TOKEN_VAR: "t"}
    code = cli_main(["talk"], out=out, err=err, in_=in_, environ=env)
    assert code == OK
    inputs = [req for req in handler.seen if req.url.path.endswith("/inputs")]
    assert [json.loads(req.content)["events"][0]["content"] for req in inputs] == ["one", "two"]
    assert out.getvalue().count("Hello there.") == 2

    from lucy_api.cli.main import build_parser

    text = build_parser().format_help()
    assert "lucy talk" in text


def test_a_hub_that_cannot_create_a_session_is_a_refusal(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={"detail": "workspace unavailable"})

    patched(handler)
    code, _, err = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert "workspace unavailable" in err


def test_a_refused_stream_is_a_refusal(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "ses_1"})
        if request.url.path.endswith("/inputs"):
            return httpx.Response(202, json={"id": "trn_1"})
        return httpx.Response(401, json={"detail": "no"})

    patched(handler)
    code, _, err = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert "event stream was refused" in err


def test_stream_done_ends_the_wait_even_without_a_completed_event(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "ses_1"})
        if request.url.path.endswith("/inputs"):
            return httpx.Response(202, json={"id": "trn_1"})
        return httpx.Response(
            200,
            text=_sse(
                ("lucy.content.text.delta", {"delta": "Hi"}),
                ("lucy.stream.done", {}),
            ),
            headers={"content-type": "text/event-stream"},
        )

    patched(handler)
    code, out, _ = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert out == "Hi\n"


def test_malformed_and_foreign_frames_are_ignored(patched) -> None:
    body = (
        "retry: 3000\n: lucy\n\n"
        "event: lucy.content.text.delta\ndata: not-json\n\n"
        "event: lucy.content.text.delta\ndata: []\n\n"
        'event: lucy.content.text.delta\ndata: {"type": "lucy.content.text.delta", '
        '"turn_id": "trn_1", "data": {"delta": 3}}\n\n'
        'event: lucy.turn.completed\ndata: {"type": "lucy.turn.completed", '
        '"turn_id": "trn_other", "data": {}}\n\n'
        'event: lucy.turn.completed\ndata: {"type": "lucy.turn.completed", '
        '"turn_id": "trn_1", "data": {}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "ses_1"})
        if request.url.path.endswith("/inputs"):
            return httpx.Response(202, json={"id": "trn_1"})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    patched(handler)
    code, out, _ = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert out == "\n"


def test_help_names_talk() -> None:
    from lucy_api.cli.main import build_parser

    text = build_parser().format_help()
    assert "lucy talk" in text


def test_a_session_without_an_id_is_a_refusal(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(201, json=[])

    patched(handler)
    code, _, err = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == REFUSED
    assert "could not start a conversation" in err


def test_an_empty_stream_still_returns(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"id": "ses_1"})
        if request.url.path.endswith("/inputs"):
            return httpx.Response(202, json={"id": "trn_1"})
        return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})

    patched(handler)
    code, out, _ = run(["talk", "hi"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert out == "\n"


def test_quiet_interactive_talk_skips_the_prompt(patched) -> None:
    patched(talking_hub())
    in_ = FakeStream(tty=True)
    in_.write("hi\n")
    in_.seek(0)
    out, err = FakeStream(tty=True), FakeStream(tty=True)
    env = {"LUCY_CONFIG": os.environ["LUCY_CONFIG"], TOKEN_VAR: "t"}
    code = cli_main(["talk", "--quiet"], out=out, err=err, in_=in_, environ=env)
    assert code == OK
    assert "you:" not in err.getvalue()
    assert out.getvalue() == ""


def test_frame_parser_accepts_bytes_and_unparseable_bodies() -> None:
    from lucy_api.cli.talk import _body, _frames, _repl

    frames = list(
        _frames(
            [
                b"event: lucy.stream.done\n",
                b"data: {}\n",
                b"\n",
            ]
        )
    )
    assert frames[0][0] == "lucy.stream.done"

    class Bad:
        def json(self) -> dict[str, object]:
            raise ValueError

    assert _body(Bad()) == {}
    ctx = type("C", (), {"in_": None, "args": type("A", (), {"quiet": True, "json": False})()})()
    assert _repl(ctx, "") == OK
    assert list(_frames(["event: lucy.stream.done", "data: {}", "no-blank"])) == []


def test_talk_without_stdin_asks_for_a_message() -> None:
    from argparse import Namespace
    from types import SimpleNamespace

    from lucy_api.cli.talk import cmd_talk

    ctx = SimpleNamespace(
        token="t",
        args=Namespace(words=(), session="", json=False, quiet=False),
        in_=None,
        interactive=False,
    )
    with pytest.raises(CliError) as raised:
        cmd_talk(ctx)  # type: ignore[arg-type]
    assert raised.value.code == USAGE
