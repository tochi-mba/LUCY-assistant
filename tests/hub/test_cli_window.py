"""`lucy context`, `lucy compact`, `lucy uncompact`, and the same from inside `lucy talk`.

A person can see how full a conversation's window is and compact it whenever they like;
automatic compaction carries on regardless. Every test runs against an in-memory hub, so
what is pinned is the client's contract: which route it calls with what, and what a person
reads back.
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import httpx
import pytest

from lucy_api.cli.base import OK, REFUSED, TOKEN_VAR, UNREACHABLE
from lucy_api.cli.main import build_parser
from lucy_api.cli.main import main as cli_main
from lucy_api.cli.window import describe, describe_compaction, status_line

REAL_CLIENT = httpx.Client

WINDOW = {
    "used_tokens": 84_000,
    "window_tokens": 200_000,
    "percent": 42,
    "warn_at_percent": 60,
    "compact_at_percent": 72,
    "tokens_until_compaction": 60_000,
    "state": "ok",
    "reclaimable_tool_results": 0,
    "summarised_turns": 0,
    "automatic_compaction": True,
    "compactions": 0,
}
COMPACTED = {
    "id": "cmp_1",
    "seq": 1,
    "turns": 6,
    "trigger": "manual",
    "keep_recent_turns": 2,
    "covers_from": 1,
    "covers_to": 12,
    "context_before": {**WINDOW, "percent": 74},
    "context_after": {**WINDOW, "percent": 31},
}


class FakeStream(io.StringIO):
    def __init__(self, *, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture(autouse=True)
def isolated_cli_environment(tmp_path, monkeypatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LUCY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.chdir(tmp_path)


def run(argv, *, environ=None, stdin="", tty=False):
    out, err = FakeStream(tty=tty), FakeStream(tty=tty)
    in_ = FakeStream(tty=tty)
    in_.write(stdin)
    in_.seek(0)
    env = {"LUCY_CONFIG": os.environ["LUCY_CONFIG"], TOKEN_VAR: "t", **(environ or {})}
    code = cli_main(argv, out=out, err=err, in_=in_, environ=env)
    return code, out.getvalue(), err.getvalue()


def _sse(*frames: tuple[str, dict[str, Any], str | None]) -> str:
    parts = ["retry: 3000\n: lucy\n\n"]
    for index, (name, body, turn) in enumerate(frames, start=1):
        payload = {"type": name, "sequence_number": index, "data": body, "turn_id": turn}
        parts.append(f"id: {index}\nevent: {name}\ndata: {json.dumps(payload)}\n\n")
    return "".join(parts)


class Hub:
    """Every route these commands touch, recording what was asked."""

    def __init__(self, **overrides: httpx.Response) -> None:
        self.seen: list[httpx.Request] = []
        self.overrides = overrides
        self.listed: list[dict[str, Any]] = [
            {"id": "cmp_2", "shown": False, "active": False},
            {"id": "cmp_1", "shown": True, "active": True},
        ]
        self.sessions: list[dict[str, Any]] = [{"id": "ses_latest"}]
        self.created = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911 - a route each
        self.seen.append(request)
        path, method = request.url.path, request.method
        key = f"{method} {path}"
        if key in self.overrides:
            return self.overrides[key]
        if method == "GET" and path == "/v1/sessions":
            return httpx.Response(200, json={"data": self.sessions})
        if method == "POST" and path == "/v1/sessions":
            self.created += 1
            return httpx.Response(201, json={"id": f"ses_new{self.created}"})
        if path.endswith("/context/window"):
            return httpx.Response(200, json=WINDOW)
        if path.endswith("/compactions"):
            return httpx.Response(200, json={"automatic": True, "data": self.listed})
        if path.endswith("/uncompact"):
            chosen = json.loads(request.content)["id"]
            return httpx.Response(
                200, json={"id": chosen, "active": False, "covers_from": 1, "covers_to": 12}
            )
        if path.endswith("/compact"):
            return httpx.Response(200, json=COMPACTED)
        if path.endswith("/inputs"):
            return httpx.Response(202, json={"id": "trn_1"})
        if path.endswith("/events"):
            return httpx.Response(
                200,
                text=_sse(
                    ("lucy.content.text.delta", {"delta": "Done."}, "trn_1"),
                    ("lucy.compaction.applied", {"turns": 4, "trigger": "auto"}, None),
                    ("lucy.compaction.applied", {"turns": 1, "trigger": "manual"}, None),
                    ("lucy.context.status", {"percent": 99}, "trn_other"),
                    ("lucy.context.status", {**WINDOW, "percent": 31}, "trn_1"),
                    ("lucy.turn.completed", {}, "trn_1"),
                ),
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(key)

    def paths(self) -> list[str]:
        return [f"{request.method} {request.url.path}" for request in self.seen]


@pytest.fixture
def hub(monkeypatch) -> Hub:
    fake = Hub()
    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(fake))
    )
    return fake


# --------------------------------------------------------------------------------------
# lucy context / compact / uncompact
# --------------------------------------------------------------------------------------


def test_context_reads_the_named_conversations_window(hub: Hub) -> None:
    code, out, err = run(["context", "-s", "ses_1"])
    assert code == OK
    assert out.strip() == (
        "42% of the window used (84,000 of 200,000 tokens). "
        "Compacts automatically at 72%, 60,000 tokens from now."
    )
    assert "ses_1" in err
    assert hub.paths() == ["GET /v1/sessions/ses_1/context/window"]


def test_context_without_a_session_reads_your_latest(hub: Hub) -> None:
    code, out, _ = run(["context", "--json"])
    assert code == OK
    assert json.loads(out)["session_id"] == "ses_latest"
    assert json.loads(out)["percent"] == 42
    listing = hub.seen[0]
    assert (listing.url.path, listing.url.params["order"], listing.url.params["limit"]) == (
        "/v1/sessions",
        "desc",
        "1",
    )


def test_with_no_conversations_there_is_nothing_to_measure(hub: Hub) -> None:
    hub.sessions = []
    code, _, err = run(["context"])
    assert code == REFUSED
    assert "no conversations yet" in err
    assert "lucy talk" in err


def test_compact_keeps_what_was_asked_and_says_what_changed(hub: Hub) -> None:
    code, out, _ = run(["compact", "-s", "ses_1", "--keep", "2"])
    assert code == OK
    assert out.strip() == (
        "Compacted turns 1-6 into a summary; the newest 2 stay word for word. "
        "Window: 74% -> 31%. `lucy uncompact` puts them back."
    )
    posted = hub.seen[-1]
    assert json.loads(posted.content) == {"keep_recent_turns": 2}


def test_compact_without_keep_lets_the_persons_setting_decide(hub: Hub) -> None:
    code, _, _ = run(["compact", "-s", "ses_1"])
    assert code == OK
    assert json.loads(hub.seen[-1].content) == {}


@pytest.mark.parametrize("keep", ["-1", "101"])
def test_a_keep_out_of_range_is_named_before_anything_is_sent(hub: Hub, keep: str) -> None:
    code, _, err = run(["compact", "-s", "ses_1", "--keep", keep])
    assert code == REFUSED
    assert "between 0 and 100" in err
    assert hub.seen == []


def test_the_hubs_refusal_is_said_in_its_own_words(monkeypatch) -> None:
    fake = Hub(
        **{
            "POST /v1/sessions/ses_1/compact": httpx.Response(
                409, json={"detail": "nothing new to compact: compaction 1 already covers it"}
            )
        }
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(fake))
    )
    code, _, err = run(["compact", "-s", "ses_1"])
    assert code == REFUSED
    assert "nothing new to compact" in err


def test_uncompact_without_an_id_undoes_the_one_the_model_is_reading(hub: Hub) -> None:
    code, out, _ = run(["uncompact", "-s", "ses_1"])
    assert code == OK
    assert json.loads(hub.seen[-1].content) == {"id": "cmp_1"}
    assert out.strip() == "Undid cmp_1: entries 1-12 are read word for word again."


def test_uncompact_with_an_id_undoes_that_one(hub: Hub) -> None:
    code, _, _ = run(["uncompact", "-s", "ses_1", "cmp_2"])
    assert code == OK
    assert hub.paths() == ["POST /v1/sessions/ses_1/uncompact"]


@pytest.mark.parametrize("listed", [[], [{"id": "cmp_1", "shown": False}], ["junk"]])
def test_with_nothing_shown_there_is_nothing_to_undo(hub: Hub, listed: list[Any]) -> None:
    hub.listed = listed
    code, _, err = run(["uncompact", "-s", "ses_1"])
    assert code == REFUSED
    assert "nothing to undo" in err


def test_an_unknown_conversation_is_named_as_such(monkeypatch) -> None:
    fake = Hub(**{"GET /v1/sessions/ses_x/context/window": httpx.Response(404, json={})})
    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(fake))
    )
    code, _, err = run(["context", "-s", "ses_x"])
    assert code == REFUSED
    assert "no such conversation" in err


def test_a_body_that_is_not_an_object_is_a_refusal_not_a_crash(monkeypatch) -> None:
    fake = Hub(
        **{
            "GET /v1/sessions/ses_1/context/window": httpx.Response(500, text="<html>"),
            "GET /v1/sessions/ses_2/context/window": httpx.Response(502, json=[1]),
        }
    )
    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(fake))
    )
    for session in ("ses_1", "ses_2"):
        code, _, err = run(["context", "-s", session])
        assert code == REFUSED
        assert "Lucy refused that" in err


def test_not_signed_in_and_unreachable_are_different_answers(monkeypatch) -> None:
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(down))
    )
    code, _, err = run(["context", "-s", "ses_1"])
    assert code == UNREACHABLE
    assert "cannot reach Lucy" in err
    code, _, err = run(["context", "-s", "ses_1"], environ={TOKEN_VAR: ""})
    assert code == REFUSED
    assert "not signed in" in err


def test_help_lists_the_three_commands() -> None:
    text = build_parser().format_help()
    for name in ("context", "compact", "uncompact"):
        assert name in text


# --------------------------------------------------------------------------------------
# What a person reads
# --------------------------------------------------------------------------------------


def test_the_description_follows_the_state_the_window_is_in() -> None:
    assert "the next turn compacts automatically" in describe({**WINDOW, "state": "compacting"})
    assert "raise max_context_tokens" in describe({**WINDOW, "state": "over"})
    off = describe({**WINDOW, "automatic_compaction": False})
    assert "Automatic compaction is off" in off
    assert "Turn 1 is read as a summary." in describe({**WINDOW, "summarised_turns": 1})
    assert "Turns 1-6 are read as a summary." in describe({**WINDOW, "summarised_turns": 6})
    assert describe({}).startswith("0% of the window used (0 of 0 tokens).")
    assert describe({**WINDOW, "percent": True}).startswith("0%"), "a bool is not a number"


def test_the_status_line_is_one_short_line() -> None:
    assert status_line(WINDOW) == "context 42% · compacts at 72%"
    assert status_line({**WINDOW, "state": "over"}) == "context 42% · over the window"


def test_a_compaction_without_figures_still_says_what_it_did() -> None:
    text = describe_compaction({"turns": 1, "context_before": None, "context_after": None})
    assert text == "Compacted turn 1 into a summary. `lucy uncompact` puts them back."


# --------------------------------------------------------------------------------------
# Inside lucy talk
# --------------------------------------------------------------------------------------


def test_a_reply_ends_with_the_window_and_any_automatic_compaction(hub: Hub) -> None:
    code, out, err = run(["talk", "-s", "ses_1", "hello"])
    assert code == OK
    assert out == "Done.\n"
    assert "compacted turns 1-4 into a summary to stay within the window" in err
    assert err.count("compacted") == 1, "a person's own compaction is not announced back"
    assert err.rstrip().endswith("context 31% · compacts at 72%")


def test_talk_json_carries_the_window(hub: Hub) -> None:
    code, out, _ = run(["talk", "--json", "-s", "ses_1", "hello"])
    assert code == OK
    assert json.loads(out)["context"]["percent"] == 31


def test_a_one_shot_command_acts_on_your_latest_conversation(hub: Hub) -> None:
    code, out, _ = run(["talk", "/context"])
    assert code == OK
    assert "42% of the window used" in out
    assert "GET /v1/sessions/ses_latest/context/window" in hub.paths()
    assert not any(path.endswith("/inputs") for path in hub.paths())


def test_at_a_prompt_commands_act_on_this_conversation_and_never_reach_lucy(hub: Hub) -> None:
    lines = "\n".join(
        [
            "/context",
            "hello",
            "/context",
            "/compact",
            "/compact 2",
            "/compact two",
            "/uncompact",
            "/session",
            "/bogus",
            "//context is a word",
            "/etc/hosts looks wrong",
            "/help",
            "/new",
            "again",
            "/quit",
            "never sent",
        ]
    )
    code, out, err = run(["talk"], stdin=lines + "\n", tty=True)
    assert code == OK

    sent = [
        json.loads(request.content)["events"][0]["content"]
        for request in hub.seen
        if request.url.path.endswith("/inputs")
    ]
    assert sent == ["hello", "/context is a word", "/etc/hosts looks wrong", "again"]
    assert "no conversation yet: say something first" in err
    assert "/bogus is not a command" in err
    assert "/compact takes how many recent turns to keep" in err
    assert "GET /v1/sessions/ses_new1/context/window" in hub.paths()
    compacted = [json.loads(r.content) for r in hub.seen if r.url.path.endswith("/compact")]
    assert compacted == [{}, {"keep_recent_turns": 2}], "bare /compact leaves it to the setting"
    assert "Undid cmp_1" in out
    assert "ses_new1" in out, "/session names the conversation in hand"
    assert "/uncompact [ID]" in out
    assert "a fresh conversation starts with your next message" in err
    assert hub.created == 2, "/new started a second conversation for the next message"


def test_a_failed_command_at_a_prompt_says_its_hint_and_carries_on(monkeypatch) -> None:
    fake = Hub(**{"GET /v1/sessions/ses_1/context/window": httpx.Response(404, json={})})
    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(fake))
    )
    code, _, err = run(["talk", "-s", "ses_1"], stdin="/context\nhello\n", tty=True)
    assert code == OK
    assert "no such conversation" in err
    assert "(`lucy context` without -s uses your latest)" in err
    assert any(request.url.path.endswith("/inputs") for request in fake.seen)


def test_session_before_anything_is_said_names_none(hub: Hub) -> None:
    code, out, _ = run(["talk"], stdin="/session\n/exit\n", tty=True)
    assert code == OK
    assert "no conversation yet" in out
