"""A hub in memory, for the eval harness's tests: the real `HttpHub` talks to it over
`httpx.MockTransport`, so every test exercises the adapter and the engine together.

It is a fake of the *public HTTP surface*, and only of the parts the harness uses. A turn
is scripted by a `Play` -- what the model does with one message -- and moves forward one
step each time it is polled, the way a real one does between two polls: running, then
parked on an approval if the play asks for one, then finished with its tool results and
reply written as transcript items.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

from lucy_api.cli.evals_hub import HttpHub

REAL_CLIENT = httpx.Client
TERMINAL = {"completed", "failed", "cancelled"}
RESTING = TERMINAL | {"input_required", "auth_required"}


@dataclass
class Play:
    """What the fake model does with one message."""

    reply: str = "Done."
    ran: tuple[tuple[str, str], ...] = ()
    """``(operation, status)`` tool results written when the turn finishes."""
    asks: tuple[str, ...] = ()
    """Operations the turn parks on for approval before it runs anything."""
    denied_reply: str = ""
    status: str = "completed"
    termination: str = "success"
    running_polls: int = 0
    forever: bool = False
    summary: str = ""
    error: str = ""
    errors: tuple[str, ...] = ()
    iterations: int = 1
    tokens: tuple[int, int, int] = (100, 10, 50)
    park_without_asks: bool = False
    rest_as: str = ""
    """A status the turn rests in instead of finishing, like ``auth_required``."""


@dataclass
class _Live:
    play: Play
    polls: int = 0
    asked: bool = False
    answers: dict[str, bool] = field(default_factory=dict)


class FakeLucy:
    """Sessions, turns, items, approvals, tools and grants, all in memory."""

    def __init__(self) -> None:
        self.plays: dict[str, Play] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.turns: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.cache_read: dict[str, int] = defaultdict(int)
        self.usage_has_cache = True
        self.page = 100
        self.version = "0.1.0"
        self.capabilities: list[dict[str, Any]] = [
            {"id": "research", "usable": True, "state": "ready", "detail": "connected"},
            {"id": "notes", "usable": True, "state": "ready", "detail": ""},
        ]
        self.models: dict[str, Any] = {
            "ready": [
                {"provider": "clyde", "models": ["haiku", "sonnet"], "local": True},
            ],
            "available": [{"provider": "anthropic", "models": [], "local": False}],
            "unavailable": [
                {"provider": "openai", "detail": "no key is configured", "models": []},
                {"provider": "groq", "detail": "", "models": []},
            ],
        }
        self.bound: set[str] = {"capabilities.use", "help.operation", "notes.search"}
        self.deferred: list[str] = ["workspace"]
        self.pack_operations = {
            "workspace": {"workspace.read", "workspace.write", "workspace.delete"},
        }
        self.permissions: list[dict[str, Any]] = [
            {"id": "workspace.change", "covers": ["workspace.write"]},
            {"id": "notes.write", "covers": ["notes.*"]},
            {"id": "broken", "covers": "not a list"},
        ]
        self.gated = {"workspace.write": "workspace.change", "notes.remember": "notes.write"}
        self.grants: set[tuple[str, str]] = set()
        self.files: dict[str, str] = {"progress.md": "# Progress\n"}
        self.fail: dict[tuple[str, str], Any] = {}
        self.invoke_bodies: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.answered: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.archived: list[str] = []
        self.granted: list[tuple[str, str]] = []
        self.revoked: list[tuple[str, str]] = []
        self._live: dict[str, _Live] = {}

    # --- wiring ------------------------------------------------------------------------

    def client(self) -> httpx.Client:
        return REAL_CLIENT(transport=httpx.MockTransport(self.handle))

    def hub(self, url: str = "http://127.0.0.1:8000", token: str = "t") -> HttpHub:
        return HttpHub(self.client(), url, token)

    def say(self, text: str, play: Play) -> None:
        self.plays[text] = play

    # --- routing -------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        failure = self.fail.get((request.method, path))
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, int):
            return httpx.Response(failure, json={"detail": f"{path} is failing on purpose"})
        body = json.loads(request.content) if request.content else {}
        return self._route(request, path, body)

    def _route(self, request: httpx.Request, path: str, body: Any) -> httpx.Response:  # noqa: PLR0911
        method = request.method
        parts = path.strip("/").split("/")
        if path == "/healthy":
            return httpx.Response(200, json={"version": self.version, "environment": "test"})
        if path == "/v1/models":
            return httpx.Response(200, json=self.models)
        if path == "/v1/capabilities":
            return httpx.Response(200, json={"data": self.capabilities})
        if path == "/v1/sessions" and method == "POST":
            return self._create(request, body)
        if parts[:2] == ["v1", "sessions"]:
            return self._session_route(request, parts, body)
        if parts[:2] == ["v1", "turns"]:
            return self._turn_route(method, parts)
        if path == "/v1/tools":
            return httpx.Response(
                200,
                json={
                    "tools": [{"name": name} for name in sorted(self.bound)],
                    "deferred": self.deferred,
                },
            )
        if parts[:2] == ["v1", "tools"]:
            return self._invoke(parts[2], body)
        if parts[:2] == ["v1", "permissions"]:
            return self._permissions(request, parts, body)
        raise AssertionError(f"unrouted {method} {path}")

    # --- sessions ------------------------------------------------------------------------

    def _create(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert request.headers.get("idempotency-key")
        session_id = f"ses_{len(self.sessions) + 1}"
        session = {"id": session_id, "profile": body.get("profile", "personal"), **body}
        self.sessions[session_id] = session
        return httpx.Response(201, json=session)

    def _session_route(self, request: httpx.Request, parts: list[str], body: Any) -> httpx.Response:
        session_id = parts[2]
        tail = parts[3] if len(parts) > 3 else ""
        if tail == "inputs":
            assert request.headers.get("idempotency-key")
            return self._input(session_id, body["events"][0])
        if tail == "items":
            return self._items(session_id, request.url.params)
        if tail == "usage":
            usage: dict[str, Any] = {"input_tokens": 0}
            if self.usage_has_cache:
                usage["turn_cache_read_tokens"] = self.cache_read[session_id]
            return httpx.Response(200, json=usage)
        assert request.method == "PATCH"
        assert body == {"archived": True}
        self.archived.append(session_id)
        return httpx.Response(200, json=self.sessions[session_id])

    def _input(self, session_id: str, event: dict[str, Any]) -> httpx.Response:
        if event["type"] == "input.message":
            turn_id = f"trn_{len(self.turns) + 1}"
            turn = {
                "id": turn_id,
                "session_id": session_id,
                "status": "queued",
                "termination": None,
                "iterations": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            }
            self.turns[turn_id] = turn
            self._live[turn_id] = _Live(play=self.plays.get(event["content"], Play()))
            self._item(session_id, turn_id, "message", "user", event["content"])
            return httpx.Response(202, json=turn)
        self.answered.append(event)
        approval_id = event["approval_id"]
        turn_id = next(
            item["turn_id"]
            for item in self.items[session_id]
            if item["type"] == "approval_request" and item["content"]["approval_id"] == approval_id
        )
        live = self._live[turn_id]
        live.answers[approval_id] = event["approved"]
        self._item(
            session_id,
            turn_id,
            "approval_response",
            "user",
            {"approval_id": approval_id, "approved": event["approved"]},
        )
        turn = self.turns[turn_id]
        if len(live.answers) == len(live.play.asks):
            turn["status"] = "queued"
        return httpx.Response(202, json=turn)

    def _items(self, session_id: str, params: httpx.QueryParams) -> httpx.Response:
        rows = self.items[session_id]
        after = params.get("after")
        start = next(i for i, row in enumerate(rows) if row["id"] == after) + 1 if after else 0
        limit = min(int(params.get("limit", "20")), self.page)
        data = rows[start : start + limit]
        return httpx.Response(
            200,
            json={
                "data": data,
                "has_more": start + limit < len(rows),
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            },
        )

    def _item(
        self, session_id: str, turn_id: str | None, kind: str, role: str, content: Any
    ) -> None:
        rows = self.items[session_id]
        rows.append(
            {
                "id": f"itm_{session_id}_{len(rows) + 1}",
                "session_id": session_id,
                "turn_id": turn_id,
                "type": kind,
                "role": role,
                "content": content,
            }
        )

    # --- turns ---------------------------------------------------------------------------

    def _turn_route(self, method: str, parts: list[str]) -> httpx.Response:
        turn_id = parts[2]
        turn = self.turns[turn_id]
        if method == "POST":
            self.cancelled.append(turn_id)
            if turn["status"] == "running":
                return httpx.Response(200, json={**turn, "cancel_requested": True})
            if turn["status"] not in TERMINAL:
                turn["status"] = "cancelled"
            return httpx.Response(200, json=turn)
        self._advance(turn)
        return httpx.Response(200, json=turn)

    def _advance(self, turn: dict[str, Any]) -> None:
        if turn["status"] in RESTING:
            return
        live = self._live[turn["id"]]
        play = live.play
        if play.forever or live.polls < play.running_polls:
            live.polls += 1
            turn["status"] = "running"
            return
        session_id = turn["session_id"]
        if play.park_without_asks:
            turn["status"] = "input_required"
            return
        if play.asks and not live.asked:
            live.asked = True
            for index, operation in enumerate(play.asks, start=1):
                self._item(
                    session_id,
                    turn["id"],
                    "approval_request",
                    "assistant",
                    {
                        "approval_id": f"apr_{turn['id']}_{index}",
                        "tool": operation,
                        "permission": "notes.write",
                        "arguments": {"title": "Drink", "body": "Prefers tea", "flag": True},
                    },
                )
            turn["status"] = "input_required"
            return
        self._finish(turn, live)

    def _finish(self, turn: dict[str, Any], live: _Live) -> None:
        play = live.play
        session_id = turn["session_id"]
        denied = any(answer is False for answer in live.answers.values())
        for operation, status in play.ran:
            shown = "denied" if denied and operation in play.asks else status
            self._item(
                session_id,
                turn["id"],
                "tool_result",
                "tool",
                {
                    "operation": operation,
                    "status": shown,
                    "summary": play.summary,
                    "error": play.error if shown != "ok" else "",
                    "note": f"because {operation}",
                },
            )
        for code in play.errors:
            self._item(session_id, turn["id"], "error", "assistant", {"code": code, "detail": ""})
        reply = play.denied_reply if denied and play.denied_reply else play.reply
        if reply:
            self._item(session_id, turn["id"], "message", "assistant", reply)
        spent_in, spent_out, cached = play.tokens
        self.cache_read[session_id] += cached
        turn.update(
            status=play.rest_as or play.status,
            termination=play.termination,
            iterations=play.iterations,
            input_tokens=spent_in,
            output_tokens=spent_out,
        )

    # --- tools and permissions -----------------------------------------------------------

    def _invoke(self, operation: str, body: dict[str, Any]) -> httpx.Response:  # noqa: PLR0911
        session_id = body["session_id"]
        arguments = body["input"]
        if operation == "capabilities.use":
            capability = arguments["id"]
            if capability in self.deferred:
                self.deferred.remove(capability)
                self.bound |= self.pack_operations.get(capability, set())
            return _steps({"status": "ok", "data": {"bound": True}})
        if operation not in self.bound:
            return httpx.Response(404, json={"detail": f"Unknown tool `{operation}`"})
        permission = self.gated.get(operation)
        if permission and (permission, f"session:{session_id}") not in self.grants:
            return httpx.Response(409, json={"detail": f"{permission} needs approval"})
        if operation in self.invoke_bodies:
            return httpx.Response(200, json=self.invoke_bodies[operation])
        if operation == "workspace.write":
            self.files[arguments["path"]] = arguments["content"]
            return _steps({"status": "ok", "data": {"path": arguments["path"], "written": True}})
        if operation == "workspace.read":
            path = arguments["path"]
            if path in self.files:
                return _steps({"status": "ok", "data": {"path": path, "content": self.files[path]}})
            return _steps({"status": "error", "data": None, "error": f"no file {path}"})
        return _steps({"status": "ok", "data": "plain text"})

    def _permissions(self, request: httpx.Request, parts: list[str], body: Any) -> httpx.Response:
        profile = request.url.params.get("profile", "")
        if request.method == "GET":
            return httpx.Response(200, json={"profile": profile, "data": self.permissions})
        if request.method == "PUT":
            grant = (body["permission"], body["profile"])
            assert body["decision"] == "allow"
            self.grants.add(grant)
            self.granted.append(grant)
            return httpx.Response(200, json=body)
        grant = (parts[2], profile)
        self.grants.discard(grant)
        self.revoked.append(grant)
        return httpx.Response(204)


def _steps(step: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"tool": "x", "steps": [step], "text": ""})


class Clock:
    """A clock that only moves when the harness sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds
