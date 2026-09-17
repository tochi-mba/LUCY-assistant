"""Shared CLI behavior binds saved credentials to their configured hub."""

from __future__ import annotations

import argparse
import io
import json
import os

import httpx
import pytest

from lucy_api.cli import base, config


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def isolated_cli_environment(tmp_path, monkeypatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LUCY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.chdir(tmp_path)


def context(*, values=None, environ=None, in_=None, err=None, **arguments):
    env = {"LUCY_CONFIG": os.environ["LUCY_CONFIG"], **(environ or {})}
    if values is not None:
        config.save_config(values, env)
    args = argparse.Namespace(
        **{
            "command": "status",
            "url": None,
            "json": False,
            "quiet": False,
            "no_color": False,
            "force": False,
            **arguments,
        }
    )
    return base.Context(
        args, out=io.StringIO(), err=err if err is not None else io.StringIO(), in_=in_, environ=env
    )


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, "saved-token"),
        ("https://saved.example/", "saved-token"),
        ("https://elsewhere.example", ""),
        ("http://saved.example", ""),
        ("https://saved.example:8443", ""),
        ("https://saved.example/other", ""),
    ],
)
def test_saved_token_is_bound_to_exact_configured_hub(override, expected) -> None:
    ctx = context(values={"url": "https://saved.example/", "token": "saved-token"}, url=override)
    assert ctx.token == expected


def test_environment_url_override_cannot_forward_a_saved_credential() -> None:
    ctx = context(
        values={"url": "https://saved.example", "token": "saved-token"},
        environ={"LUCY_URL": "https://other.example"},
    )
    assert ctx.url == "https://other.example"
    assert ctx.token == ""


def test_explicit_environment_token_can_authenticate_an_overridden_hub() -> None:
    ctx = context(
        values={"url": "https://saved.example", "token": "saved-token"},
        environ={"LUCY_TOKEN": " explicit-token "},
        url="https://other.example",
    )
    assert ctx.token == "explicit-token"


def test_saved_token_without_a_named_hub_is_never_used() -> None:
    ctx = context(values={"token": "unbound-secret"})
    assert ctx.token == ""


@pytest.mark.parametrize("force", [False, True])
def test_setup_force_recovers_an_unreadable_config_without_overwriting_it(force) -> None:
    path = config.config_path(os.environ)
    path.write_text('token="secret-unclosed', encoding="utf-8")
    if force:
        ctx = context(command="setup", force=True)
        assert ctx.config.exists
        assert ctx.config.values == {}
        assert ctx.token == ""
    else:
        with pytest.raises(base.CliError, match="not valid UTF-8 TOML") as raised:
            context(command="setup", force=False)
        assert raised.value.code == base.USAGE
        assert "setup --force" in raised.value.hint
        assert "secret-unclosed" not in str(raised.value)
    assert path.read_text(encoding="utf-8") == 'token="secret-unclosed'


def test_bad_configuration_for_another_command_keeps_the_recovery_hint() -> None:
    config.config_path(os.environ).write_text("broken =", encoding="utf-8")
    with pytest.raises(base.CliError, match="not valid UTF-8 TOML") as raised:
        context(command="status")
    assert "setup --force" in raised.value.hint


@pytest.mark.parametrize(
    "url",
    [
        "http://",
        "https://user:secret@host",
        "https://host/?token=secret",
        "https://host/#secret",
        "http://host:0",
        "http://host:65536",
        "http://host:bad",
        "http://[broken",
        "http://host/ space",
        "http://host/\x00",
        "http://host/\n",
    ],
)
def test_invalid_hub_urls_are_refused_without_echoing_potential_credentials(url) -> None:
    with pytest.raises(base.CliError) as raised:
        base.resolve_url(url, {})
    assert raised.value.code == base.USAGE
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "token", ["two words", "first\nsecond", "carriage\rreturn", "zero\x00byte", "é", "\x7f"]
)
def test_tokens_with_header_injection_or_non_ascii_are_refused(token) -> None:
    with pytest.raises(base.CliError, match="printable ASCII") as raised:
        base.resolve_token({"LUCY_TOKEN": token})
    assert token not in str(raised.value)


@pytest.mark.parametrize(
    ("input_terminal", "error_terminal", "json_mode", "expected"),
    [
        (True, True, False, True),
        (False, True, False, False),
        (True, False, False, False),
        (True, True, True, False),
    ],
)
def test_interaction_requires_both_terminals_and_human_output(
    input_terminal, error_terminal, json_mode, expected
) -> None:
    ctx = context(
        in_=Terminal() if input_terminal else io.StringIO(),
        err=Terminal() if error_terminal else io.StringIO(),
        json=json_mode,
    )
    assert ctx.interactive is expected


def test_terminal_detection_handles_absent_stream_methods() -> None:
    assert base.is_terminal(None) is False
    assert base.is_terminal(object()) is False
    assert base.wants_colour(object(), no_color=False, environ={}) is False


@pytest.mark.parametrize(
    ("quiet", "json_mode", "expected"),
    [(False, False, "message\n"), (True, False, ""), (False, True, "")],
)
def test_commentary_only_reaches_human_stderr(quiet, json_mode, expected) -> None:
    ctx = context(quiet=quiet, json=json_mode)
    ctx.say("message")
    assert ctx.err.getvalue() == expected
    assert ctx.out.getvalue() == ""


def test_quiet_preserves_machine_readable_results() -> None:
    ctx = context(quiet=True, json=True)
    ctx.emit({"answer": 42}, "human answer")
    assert json.loads(ctx.out.getvalue()) == {"answer": 42}


def test_warning_style_uses_amber_when_enabled() -> None:
    assert base.Style(enabled=True).warn("careful") == "\x1b[33mcareful\x1b[0m"
    assert base.Style(enabled=False).warn("careful") == "careful"


def test_transport_failures_expose_only_the_exception_type() -> None:
    def unavailable(request):
        raise httpx.ConnectError("secret-header-and-body", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(unavailable)) as client,
        pytest.raises(base.CliError) as raised,
    ):
        base.fetch(client, base.DEFAULT_URL, "/healthy", "fake-token")
    assert raised.value.code == base.UNREACHABLE
    assert "ConnectError" in raised.value.hint
    assert "secret-header-and-body" not in str(raised.value) + raised.value.hint
