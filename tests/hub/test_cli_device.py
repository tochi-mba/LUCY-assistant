"""The CLI follows RFC device polling without exposing the resulting token."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from lucy_api.cli.base import CliError, Context
from lucy_api.cli.device import sign_in
from lucy_api.cli.main import build_parser
from lucy_api.cli.main import main as cli_main

REAL_CLIENT = httpx.Client


def _context(tmp_path) -> Context:
    args = build_parser().parse_args(["setup", "--mode", "remote"])
    return Context(
        args,
        out=io.StringIO(),
        err=io.StringIO(),
        in_=io.StringIO(),
        environ={"LUCY_CONFIG": str(tmp_path / "config.toml")},
    )


def _install(monkeypatch, replies):
    queue = iter(replies)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_: REAL_CLIENT(transport=httpx.MockTransport(lambda _request: next(queue))),
    )
    monkeypatch.setattr("lucy_api.cli.device.time.sleep", lambda _seconds: None)
    opened = []
    monkeypatch.setattr(
        "lucy_api.cli.device.webbrowser.open",
        lambda url, **_kwargs: opened.append(url) or True,
    )
    return opened


def test_sign_in_opens_the_browser_handles_pending_and_returns_the_token(
    tmp_path, monkeypatch
) -> None:
    opened = _install(
        monkeypatch,
        [
            httpx.Response(
                201,
                json={
                    "device_code": "device-secret-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://hub.test/device?user_code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "header.payload.signature"}),
        ],
    )
    context = _context(tmp_path)

    token = sign_in(context, "https://hub.test")

    assert token == "header.payload.signature"
    assert opened == ["https://hub.test/device?user_code=ABCD-EFGH"]
    assert token not in context.err.getvalue()
    said = context.err.getvalue()
    assert "lucy approve ABCD-EFGH" in said
    assert "--token-stdin" in said


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(404),
        httpx.Response(201, json={}),
    ],
)
def test_sign_in_refuses_an_unsupported_or_malformed_hub(tmp_path, monkeypatch, reply) -> None:
    _install(monkeypatch, [reply])
    with pytest.raises(CliError):
        sign_in(_context(tmp_path), "https://hub.test")


def test_sign_in_reports_terminal_device_errors(tmp_path, monkeypatch) -> None:
    _install(
        monkeypatch,
        [
            httpx.Response(
                201,
                json={
                    "device_code": "device-secret-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://hub.test/device",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
            httpx.Response(400, json={"error": "access_denied"}),
        ],
    )
    with pytest.raises(CliError, match="denied or expired"):
        sign_in(_context(tmp_path), "https://hub.test")


def test_sign_in_reports_an_unrecognized_device_error(tmp_path, monkeypatch) -> None:
    _install(
        monkeypatch,
        [
            httpx.Response(
                201,
                json={
                    "device_code": "device-secret-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://hub.test/device",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
            httpx.Response(400, json={"error": "server_error"}),
        ],
    )
    with pytest.raises(CliError, match="could not complete"):
        sign_in(_context(tmp_path), "https://hub.test")


def test_sign_in_expires_when_the_window_closes(tmp_path, monkeypatch) -> None:
    ticks = iter([0.0, 10.0])
    monkeypatch.setattr("lucy_api.cli.device.time.monotonic", lambda: next(ticks))
    _install(
        monkeypatch,
        [
            httpx.Response(
                201,
                json={
                    "device_code": "device-secret-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://hub.test/device",
                    "expires_in": 1,
                    "interval": 1,
                },
            )
        ],
    )
    with pytest.raises(CliError, match="expired"):
        sign_in(_context(tmp_path), "https://hub.test")


def test_sign_in_names_a_network_failure(tmp_path, monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_: REAL_CLIENT(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(CliError, match="cannot reach"):
        sign_in(_context(tmp_path), "https://hub.test")


def test_a_poll_whose_connection_the_hub_dropped_is_asked_again(tmp_path, monkeypatch) -> None:
    """The bug, named: the hub closes a connection idle for as long as the poll interval,
    a poll landed on one closing under it, and sign-in ended as "cannot reach Lucy" while
    the hub was up and waiting for the approval."""
    replies = iter(
        [
            httpx.Response(
                201,
                json={
                    "device_code": "device-secret-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://hub.test/device?user_code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            ),
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            httpx.Response(200, json={"access_token": "header.payload.signature"}),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr("lucy_api.cli.device.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("lucy_api.cli.device.webbrowser.open", lambda *_a, **_k: True)

    assert sign_in(_context(tmp_path), "https://hub.test") == "header.payload.signature"


# --- lucy approve -----------------------------------------------------------------------------


def _approve(monkeypatch, tmp_path, reply, *argv: str, token: str = "approver.jwt"):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(handler))
    )
    out, err = io.StringIO(), io.StringIO()
    environ = {"LUCY_CONFIG": str(tmp_path / "config.toml"), "LUCY_URL": "https://hub.test"}
    if token:
        environ["LUCY_TOKEN"] = token
    code = cli_main(["approve", *argv], out=out, err=err, in_=io.StringIO(), environ=environ)
    return code, out.getvalue(), err.getvalue(), seen


def test_approving_hands_this_client_s_sign_in_to_the_waiting_one(tmp_path, monkeypatch) -> None:
    code, out, _err, seen = _approve(monkeypatch, tmp_path, httpx.Response(204), "abcd-efgh")
    assert code == 0
    assert "Approved: the client showing ABCD-EFGH is now signed in as you." in out
    [request] = seen
    assert request.url.path == "/v1/auth/device/authorize"
    assert json.loads(request.content) == {"user_code": "ABCD-EFGH", "approve": True}
    assert request.headers["Authorization"] == "Bearer approver.jwt"


def test_denying_says_the_waiting_client_will_not_be_signed_in(tmp_path, monkeypatch) -> None:
    code, out, _err, seen = _approve(
        monkeypatch, tmp_path, httpx.Response(204), "ABCD-EFGH", "--deny"
    )
    assert code == 0
    assert "Refused: the client showing ABCD-EFGH will not be signed in." in out
    assert json.loads(seen[0].content)["approve"] is False


@pytest.mark.parametrize(
    ("reply", "exit_code", "said"),
    [
        (httpx.Response(401), 2, "the hub refused this client's own token"),
        (
            httpx.Response(
                400,
                json={
                    "error": "expired_token",
                    "error_description": "The device code has expired.",
                },
            ),
            1,
            "the hub did not take code ABCD-EFGH: The device code has expired.",
        ),
        (httpx.Response(500), 1, "the hub did not take code ABCD-EFGH: HTTP 500"),
        (httpx.ConnectError("down"), 3, "cannot reach Lucy at https://hub.test"),
    ],
)
def test_an_approval_the_hub_did_not_take_says_why(
    tmp_path, monkeypatch, reply, exit_code, said
) -> None:
    code, _out, err, _seen = _approve(monkeypatch, tmp_path, reply, "ABCD-EFGH")
    assert code == exit_code
    assert said in err


def test_a_client_that_is_not_signed_in_has_nothing_to_approve_with(tmp_path, monkeypatch) -> None:
    code, _out, err, seen = _approve(monkeypatch, tmp_path, httpx.Response(204), "X", token="")
    assert code == 2
    assert "this client is not signed in" in err
    assert seen == []
