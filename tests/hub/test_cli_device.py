"""The CLI follows RFC device polling without exposing the resulting token."""

from __future__ import annotations

import io

import httpx
import pytest

from lucy_api.cli.base import CliError, Context
from lucy_api.cli.device import sign_in
from lucy_api.cli.main import build_parser

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
