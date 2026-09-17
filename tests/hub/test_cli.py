"""The `lucy` command's contract.

A CLI is a user interface and an API for scripts, so both halves are tested: what a person
reads, and what a script can depend on — the exit code, the stream, and the JSON.
"""

from __future__ import annotations

import io
import json
import os

import httpx
import pytest

from lucy_api import __version__
from lucy_api.cli import main as cli_main
from lucy_api.cli.base import (
    DEFAULT_URL,
    OK,
    REFUSED,
    TOKEN_VAR,
    UNREACHABLE,
    URL_VAR,
    USAGE,
    CliError,
    Style,
    fetch,
    resolve_url,
    wants_colour,
)
from lucy_api.cli.main import build_parser


@pytest.fixture(autouse=True)
def isolated_cli_environment(tmp_path, monkeypatch) -> None:
    for key in tuple(os.environ):
        if key.startswith("LUCY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("LUCY_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.chdir(tmp_path)


class FakeStream(io.StringIO):
    """A stream that can claim to be a terminal, which is what colour turns on."""

    def __init__(self, *, tty: bool = False) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def run(argv, *, environ=None, tty=False):
    out, err = FakeStream(tty=tty), FakeStream(tty=tty)
    env = {"LUCY_CONFIG": os.environ["LUCY_CONFIG"], **(environ or {})}
    code = cli_main(argv, out=out, err=err, environ=env)
    return code, out.getvalue(), err.getvalue()


# Captured before any test replaces `httpx.Client`, because the fake hub is itself built
# from a real client and would otherwise call whatever the monkeypatch installed.
REAL_CLIENT = httpx.Client


def hub(handler):
    """Point the CLI's httpx at an in-memory hub."""
    return REAL_CLIENT(transport=httpx.MockTransport(handler))


def a_healthy_hub(*, ready=True, account=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthy":
            return httpx.Response(200, json={"status": "alive", "version": "9.9.9"})
        if request.url.path == "/ready":
            status = "ok" if ready else "degraded"
            return httpx.Response(
                200 if ready else 503,
                json={"status": status, "checks": {"keyring": {"status": status}}},
            )
        if request.url.path == "/v1/me":
            if account is None:
                return httpx.Response(401, json={"detail": "a keyring token is required"})
            return httpx.Response(200, json={"account_id": account, "audience": "lucy-api"})
        raise AssertionError(request.url.path)

    return handler


@pytest.fixture
def patched(monkeypatch):
    """Install a fake hub for every httpx.Client the CLI opens."""

    def install(handler):
        monkeypatch.setattr(httpx, "Client", lambda **_: hub(handler))

    return install


def test_no_command_prints_help_to_stdout_and_succeeds() -> None:
    code, out, err = run([])
    assert code == OK
    assert "examples:" in out
    assert "lucy status" in out
    assert err == ""


def test_help_leads_with_examples_and_names_the_exit_codes() -> None:
    parser = build_parser()
    text = parser.format_help()
    assert text.index("examples:") < text.index("exit codes:")
    for line in ("0 it worked", "2 bad command", "3 hub unreachable"):
        assert line in text
    assert "Never pass a token as a flag" in text


def test_a_bad_command_is_argparse_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_:
        run(["nonsense"])
    assert exit_.value.code == 2


def test_status_reads_ready_and_says_who_you_are(patched) -> None:
    patched(a_healthy_hub(account="acct_a"))
    code, out, err = run(["status"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert "alive   yes" in out
    assert "ready   yes" in out
    assert "keyring" in out
    assert "acct_a" in out
    assert err == ""


def test_status_answers_no_when_a_dependency_is_unusable(patched) -> None:
    patched(a_healthy_hub(ready=False))
    code, out, _ = run(["status"])
    assert code == REFUSED
    assert "ready   no" in out
    assert "a dependency is unusable" in out


def test_status_json_is_the_shape_a_script_depends_on(patched) -> None:
    patched(a_healthy_hub(account="acct_a"))
    code, out, _ = run(["status", "--json"], environ={TOKEN_VAR: "t"})
    assert code == OK
    assert json.loads(out) == {
        "url": DEFAULT_URL,
        "alive": True,
        "ready": True,
        "checks": {"keyring": "ok"},
        "account_id": "acct_a",
    }


def test_status_distinguishes_no_token_from_a_refused_one(patched) -> None:
    patched(a_healthy_hub())
    _, without, _ = run(["status"])
    assert "not signed in" in without
    _, refused, _ = run(["status"], environ={TOKEN_VAR: "bad"})
    assert "was refused" in refused


def test_an_unreachable_hub_is_its_own_exit_code_and_goes_to_stderr(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    monkeypatch.setattr(httpx, "Client", lambda **_: hub(refuse))
    code, out, err = run(["status"])
    assert code == UNREACHABLE
    assert out == ""
    assert "cannot reach Lucy" in err
    assert "lucy serve" in err


def test_version_reports_both_and_survives_a_missing_hub(patched, monkeypatch) -> None:
    patched(a_healthy_hub())
    code, out, _ = run(["version"])
    assert code == OK
    assert __version__ in out
    assert "9.9.9" in out

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    monkeypatch.setattr(httpx, "Client", lambda **_: hub(refuse))
    code, out, _ = run(["--version"])
    assert code == OK
    assert "not running" in out


def test_version_ignores_a_hub_that_answers_but_not_with_a_version(patched) -> None:
    patched(lambda request: httpx.Response(500, json={"detail": "broken"}))
    code, out, _ = run(["version"])
    assert code == OK
    assert "not running" in out


def test_quiet_prints_nothing_but_still_answers_with_its_code(patched) -> None:
    patched(a_healthy_hub(ready=False))
    code, out, _ = run(["status", "--quiet"])
    assert code == REFUSED
    assert out == ""


def test_quiet_does_not_suppress_json_because_a_script_asked_for_it(patched) -> None:
    patched(a_healthy_hub())
    code, out, _ = run(["status", "--quiet", "--json"])
    assert code == OK
    assert json.loads(out)["alive"] is True


@pytest.mark.parametrize(
    ("tty", "environ", "no_color", "expected"),
    [
        (True, {}, False, True),
        (False, {}, False, False),
        (True, {"NO_COLOR": ""}, False, False),
        (True, {"TERM": "dumb"}, False, False),
        (True, {}, True, False),
    ],
)
def test_colour_is_off_whenever_anything_says_so(tty, environ, no_color, expected) -> None:
    assert wants_colour(FakeStream(tty=tty), no_color=no_color, environ=environ) is expected


def test_style_is_a_no_op_when_colour_is_off() -> None:
    plain, colour = Style(enabled=False), Style(enabled=True)
    assert plain.good("yes") == "yes"
    assert plain.bad("no") == "no"
    assert plain.dim("x") == "x"
    assert colour.good("yes").startswith("\033[32m")
    assert colour.bad("no").startswith("\033[31m")
    assert colour.dim("x").startswith("\033[2m")


def test_colour_reaches_a_terminal_and_never_a_pipe(patched) -> None:
    patched(a_healthy_hub())
    _, piped, _ = run(["status"])
    assert "\033[" not in piped
    _, terminal, _ = run(["status"], tty=True)
    assert "\033[" in terminal


@pytest.mark.parametrize(
    ("flag", "environ", "expected"),
    [
        (None, {}, DEFAULT_URL),
        (None, {URL_VAR: "http://box:9000"}, "http://box:9000"),
        ("http://flag:1/", {URL_VAR: "http://box:9000"}, "http://flag:1"),
    ],
)
def test_the_url_resolves_flag_then_environment_then_this_machine(flag, environ, expected) -> None:
    assert resolve_url(flag, environ) == expected


@pytest.mark.parametrize(
    "argv",
    [["status", "--json"], ["--json", "status"], ["status", "--json", "--quiet"]],
)
def test_a_global_flag_works_on_either_side_of_the_subcommand(patched, argv) -> None:
    patched(a_healthy_hub())
    code, out, _ = run(argv)
    assert code == OK
    assert json.loads(out)["alive"] is True


@pytest.mark.parametrize("bad", ["box:8000", "localhost", "ftp://box"])
def test_an_address_without_a_scheme_is_refused_before_the_socket(bad) -> None:
    code, out, err = run(["status"], environ={URL_VAR: bad})
    assert code == USAGE
    assert out == ""
    assert "must start with http:// or https://" in err
    assert bad not in err
    assert err.count("\n") == 1, "an error carrying its own fix gets no second hint line"


def test_the_token_never_appears_in_the_help_or_in_a_flag() -> None:
    text = build_parser().format_help()
    assert "--token" not in text


def test_a_token_is_sent_as_a_bearer_header_and_an_absent_one_is_not() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={})

    with hub(handler) as client:
        fetch(client, DEFAULT_URL, "/healthy", "abc")
        fetch(client, DEFAULT_URL, "/healthy", "")
    assert seen == ["Bearer abc", None]


def test_ready_without_json_content_type_is_not_parsed(patched) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ready":
            return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})
        return a_healthy_hub()(request)

    patched(handler)
    code, out, _ = run(["status", "--json"])
    assert code == OK
    assert json.loads(out)["checks"] == {}


def test_serve_starts_the_hub_on_the_asked_for_address(monkeypatch) -> None:
    import lucy_api.__main__ as entry

    started: dict[str, str] = {}
    monkeypatch.setattr(entry, "main", lambda: started.update(os_env()))

    def os_env():
        import os

        return {"host": os.environ["LUCY_HOST"], "port": os.environ["LUCY_PORT"]}

    code, _, err = run(["serve", "--host", "127.0.0.2", "--port", "8123"])
    assert code == OK
    assert started == {"host": "127.0.0.2", "port": "8123"}
    assert "http://127.0.0.2:8123" in err


def test_json_makes_a_failure_machine_readable_on_stderr(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    monkeypatch.setattr(httpx, "Client", lambda **_: hub(refuse))
    code, out, err = run(["status", "--json"])
    assert code == UNREACHABLE
    assert out == "", "stdout carries the answer, never the error"
    failure = json.loads(err)["error"]
    assert failure["exit_code"] == UNREACHABLE
    assert "cannot reach Lucy" in failure["message"]
    assert "lucy serve" in failure["hint"]


def test_a_json_failure_with_no_hint_says_so_rather_than_omitting_the_key() -> None:
    code, _, err = run(["status", "--json"], environ={URL_VAR: "box:8000"})
    assert code == USAGE
    assert json.loads(err)["error"]["hint"] is None


def test_a_cli_error_carries_its_code_and_hint() -> None:
    error = CliError("broken", UNREACHABLE, hint="try this")
    assert error.code == UNREACHABLE
    assert error.hint == "try this"
    assert str(error) == "broken"


def test_status_accepts_an_unreadable_checks_field_without_crashing(patched) -> None:
    def handler(request):
        if request.url.path == "/ready":
            return httpx.Response(503, json={"checks": []})
        return a_healthy_hub()(request)

    patched(handler)
    code, out, _ = run(["status", "--json"])
    assert code == REFUSED
    assert json.loads(out)["checks"] == {}


@pytest.mark.parametrize("argv", [["-V", "status"], ["status", "-V"], ["status", "--version"]])
def test_version_flag_overrides_subcommand_on_either_side(patched, argv) -> None:
    patched(a_healthy_hub())
    code, out, err = run(argv)
    assert code == OK
    assert __version__ in out
    assert "alive" not in out
    assert err == ""


def test_serve_respects_existing_environment_when_flags_are_absent(monkeypatch) -> None:
    import lucy_api.__main__ as entry

    monkeypatch.setenv("LUCY_HOST", "127.0.0.3")
    monkeypatch.setenv("LUCY_PORT", "8124")
    started = []
    monkeypatch.setattr(entry, "main", lambda: started.append(True))
    code, _, err = run(["serve"])
    assert code == OK
    assert started == [True]
    assert "http://127.0.0.3:8124" in err
    assert os.environ["LUCY_HOST"] == "127.0.0.3"
    assert os.environ["LUCY_PORT"] == "8124"


@pytest.mark.parametrize("failure", [RuntimeError("server-secret"), ValueError("server-secret")])
def test_serve_reports_configuration_failure_without_echoing_values(monkeypatch, failure) -> None:
    from lucy_api.core import config

    def unusable():
        raise failure

    monkeypatch.setattr(config, "load_settings", unusable)
    code, out, err = run(["serve"])
    assert code == USAGE
    assert out == ""
    assert "configuration is not usable" in err
    assert "check your .env file" in err
    assert "server-secret" not in err


def test_ctrl_c_is_an_exit_code_and_not_a_traceback(monkeypatch) -> None:
    from lucy_api.cli.base import INTERRUPTED

    def interrupted(request):
        raise KeyboardInterrupt

    monkeypatch.setattr(httpx, "Client", lambda **_: hub(interrupted))
    code, out, err = run(["status"])
    assert code == INTERRUPTED
    assert out == ""
    assert err == "\n"


def test_main_can_use_standard_streams_and_environment(monkeypatch, capsys, patched) -> None:
    patched(a_healthy_hub())
    monkeypatch.setenv("LUCY_URL", "http://environment.example:8123")
    assert cli_main(["status", "--json"]) == OK
    captured = capsys.readouterr()
    assert json.loads(captured.out)["url"] == "http://environment.example:8123"
    assert captured.err == ""


def test_status_reports_failed_liveness(patched) -> None:
    def handler(request):
        if request.url.path == "/healthy":
            return httpx.Response(503, json={})
        return a_healthy_hub(ready=False)(request)

    patched(handler)
    code, out, _ = run(["status"])
    assert code == REFUSED
    assert "alive   no" in out


def test_authentication_outage_does_not_claim_the_token_is_invalid(patched) -> None:
    def handler(request):
        if request.url.path == "/v1/me":
            return httpx.Response(503, json={"detail": "dependency unavailable"})
        return a_healthy_hub(ready=False)(request)

    patched(handler)
    code, out, err = run(["status"], environ={TOKEN_VAR: "header.payload.signature"})
    assert code == REFUSED
    assert "refused" not in out
    assert "identity could not be checked" in out
    assert err == ""


@pytest.mark.parametrize("path", ["/ready", "/v1/me"])
@pytest.mark.parametrize("body", [b"secret-malformed", b"[]"])
def test_status_handles_malformed_json_without_a_traceback_or_body_leak(
    patched, path, body
) -> None:
    def handler(request):
        if request.url.path == path:
            return httpx.Response(200, content=body, headers={"content-type": "application/json"})
        return a_healthy_hub()(request)

    patched(handler)
    code, out, err = run(["status", "--json"])
    assert code in {OK, REFUSED, USAGE}
    assert "secret-malformed" not in out + err
    assert "Traceback" not in err
    assert json.loads(out or err)


@pytest.mark.parametrize("body", [b"secret-malformed", b"[]"])
def test_version_handles_malformed_server_json_without_echoing_it(patched, body) -> None:
    patched(lambda request: httpx.Response(200, content=body))
    code, out, err = run(["version", "--json"])
    assert code == OK
    assert json.loads(out) == {"client": __version__, "hub": None}
    assert "secret-malformed" not in out + err
