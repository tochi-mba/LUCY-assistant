"""First-run behaviour, including interrupted setup and unattended use."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from lucy_api.cli import main
from lucy_api.cli.base import CliError, Context
from lucy_api.cli.config import ConfigError, load_config, save_config
from lucy_api.cli.main import build_parser
from lucy_api.cli.setup import MAX_TOKEN_CHARS, _setup_token

REAL_CLIENT = httpx.Client
SECRET = "header.payload.signature"


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def env(tmp_path):
    return {
        "LUCY_CONFIG": str(tmp_path / "client.toml"),
        "LUCY_FAMILY_ROOT": str(tmp_path),
    }


def run(argv, env, *, text="", tty=False):
    stream = Terminal if tty else io.StringIO
    out, err, stdin = stream(), stream(), stream(text)
    code = main(argv, out=out, err=err, in_=stdin, environ=env)
    return code, out.getvalue(), err.getvalue()


def install(monkeypatch, handler):
    monkeypatch.setattr(
        httpx, "Client", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(handler))
    )


def install_device(monkeypatch, *, token=SECRET):
    def handler(request):
        if request.url.path == "/v1/auth/device":
            return httpx.Response(
                201,
                json={
                    "device_code": "device-secret-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://hub.example/device?user_code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})

    install(monkeypatch, handler)
    monkeypatch.setattr("lucy_api.cli.device.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("lucy_api.cli.device.webbrowser.open", lambda *_args, **_kwargs: True)


def service(**overrides):
    return {
        "id": "music",
        "title": "Music",
        "state": "ready",
        "connection_state": "unknown",
        "summary": "Ready does not mean connected.",
        "actions": [{"description": "Connect Spotify in Keyring once."}],
        **overrides,
    }


def test_unattended_setup_requires_a_choice_and_never_prompts(env):
    code, out, err = run(["setup"], env)
    assert code == 2
    assert "explicit options" in err
    assert not out
    assert not load_config(env).exists


@pytest.mark.parametrize("mode", ["hub", "family", "remote"])
def test_each_mode_saves_the_address_and_names_the_next_step(env, mode):
    code, out, err = run(
        [
            "setup",
            "--mode",
            mode,
            "--url",
            "https://hub.example",
            "--no-token",
            "--no-github-ci",
            "--json",
        ],
        env,
    )
    assert code == 0
    assert not err
    assert json.loads(out)["saved"]
    assert "already" in json.loads(out)
    assert load_config(env).values == {"url": "https://hub.example", "mode": mode}


def test_remote_mode_requires_an_address(env):
    code, _, err = run(["setup", "--mode", "remote"], env)
    assert code == 2
    assert "--url" in err


def test_yes_selects_the_default_and_never_prompts(env):
    assert run(["setup", "--yes", "--json"], env)[0] == 0
    assert load_config(env).get("mode") == "hub"


def test_dry_run_never_reads_a_token_writes_or_contacts_the_hub(env):
    code, out, _ = run(
        ["setup", "--dry-run", "--token-stdin", "--capabilities", "--json"], env, text=SECRET
    )
    assert code == 0
    assert not json.loads(out)["saved"]
    assert SECRET not in out
    assert not load_config(env).exists


def test_a_saved_token_is_preserved_on_a_same_hub_preview(env):
    save_config({"url": "http://127.0.0.1:8000", "token": SECRET, "mode": "hub"}, env)
    code, out, _ = run(["setup", "--dry-run", "--json"], env)
    assert code == 0
    assert json.loads(out)["token_saved"]


def test_overwriting_is_explicit_and_a_url_change_drops_the_saved_token(env):
    save_config({"url": "http://old.example", "token": SECRET, "mode": "hub"}, env)
    code, out, _ = run(["setup", "--yes", "--json"], env)
    assert code == 0
    payload = json.loads(out)
    assert payload["kept"]
    assert not payload["saved"]
    assert payload["url"] == "http://old.example"
    assert any(row["id"] == "config" and row["done"] for row in payload["already"])
    assert run(["setup", "--force", "--url", "https://new.example"], env)[0] == 0
    assert load_config(env).get("token") == ""
    assert load_config(env).get("url") == "https://new.example"


def test_no_token_removes_an_existing_saved_credential(env):
    save_config({"url": "http://127.0.0.1:8000", "token": SECRET}, env)
    assert run(["setup", "--yes", "--force", "--no-token"], env)[0] == 0
    assert load_config(env).get("token") == ""


@pytest.mark.parametrize("text", ["", "x" * (MAX_TOKEN_CHARS + 1), "first\nsecond", "a b"])
def test_bad_token_input_never_writes_a_file(env, text):
    code, out, _ = run(["setup", "--yes", "--token-stdin"], env, text=text)
    assert code == 2
    assert SECRET not in out
    assert not load_config(env).exists


def test_a_piped_token_is_saved_without_echoing_it(env):
    code, out, err = run(["setup", "--yes", "--token-stdin"], env, text=SECRET + "\n")
    assert code == 0
    assert load_config(env).get("token") == SECRET
    assert SECRET not in out + err


def test_a_direct_command_without_stdin_reports_missing_input(env):
    args = build_parser().parse_args(["setup", "--token-stdin"])
    ctx = Context(args, out=io.StringIO(), err=io.StringIO(), environ=env)
    with pytest.raises(CliError, match="no token input"):
        _setup_token(ctx, ctx.url)


def test_environment_overrides_are_reported_but_never_saved_as_credentials(env):
    env.update(LUCY_URL="https://env.example", LUCY_TOKEN=SECRET)
    code, out, _ = run(["setup", "--yes", "--json"], env)
    assert code == 0
    assert len(json.loads(out)["notices"]) == 3
    assert load_config(env).get("token") == ""
    assert SECRET not in out


def test_interactive_setup_uses_browser_device_sign_in(env, monkeypatch):
    install_device(monkeypatch)
    code, out, err = run(["setup"], env, text="remote\nhttps://chosen.example\n", tty=True)
    assert code == 0
    assert load_config(env).get("url") == "https://chosen.example"
    assert load_config(env).get("token") == SECRET
    assert SECRET not in out + err


@pytest.mark.parametrize("text", ["", "wrong\n"])
def test_eof_or_an_invalid_mode_stops_setup_without_writing(env, text):
    assert run(["setup"], env, text=text, tty=True)[0] in (1, 2)
    assert not load_config(env).exists


def test_interactive_replacement_defaults_to_keeping_what_is_already_set_up(env):
    save_config({"mode": "hub", "url": "http://127.0.0.1:8000"}, env)
    code, out, _ = run(["setup"], env, text="\n", tty=True)
    assert code == 0
    assert load_config(env).get("url") == "http://127.0.0.1:8000"
    assert "Setup" in out
    assert "done" in out


def test_interactive_replacement_refreshes_the_saved_token_in_the_browser(env, monkeypatch):
    save_config({"mode": "hub", "url": "http://127.0.0.1:8000", "token": SECRET}, env)
    install_device(monkeypatch, token="fresh.header.signature")
    assert run(["setup"], env, text="yes\n\n", tty=True)[0] == 0
    assert load_config(env).get("token") == "fresh.header.signature"


def test_a_failed_write_is_a_cli_error(env, monkeypatch):
    def fail(*args):
        raise ConfigError("cannot write configuration")

    monkeypatch.setattr("lucy_api.cli.setup.save_config", fail)
    assert run(["setup", "--yes"], env)[0] == 2


def test_capability_discovery_failure_preserves_the_saved_configuration(env):
    code, out, _ = run(["setup", "--yes", "--capabilities", "--json"], env)
    assert code == 1
    assert json.loads(out)["saved"]
    assert load_config(env).exists


def test_capabilities_are_included_in_one_setup_result(env, monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(200, json={"services": [service()]}))
    code, out, err = run(
        ["setup", "--yes", "--token-stdin", "--capabilities", "--json"], env, text=SECRET
    )
    assert code == 0
    assert not err
    assert json.loads(out)["services"][0]["id"] == "music"


@pytest.mark.parametrize(
    "flags,extra,source",
    [
        ([], {}, "default"),
        ([], {"LUCY_URL": "https://env.example"}, "environment"),
        (["--url", "https://flag.example"], {}, "flag"),
    ],
)
def test_config_reports_the_effective_source(env, flags, extra, source):
    code, out, _ = run(["config", "--json", *flags], {**env, **extra})
    assert code == 0
    assert json.loads(out)["url"]["source"] == source


def test_config_redacts_file_and_environment_tokens(env):
    save_config({"url": "http://127.0.0.1:8000", "token": SECRET}, env)
    _, out, _ = run(["config", "--json"], env)
    assert json.loads(out)["token"]["source"] == "file"
    assert json.loads(out)["url"]["source"] == "file"
    _, out, _ = run(["config"], {**env, "LUCY_TOKEN": SECRET})
    assert SECRET not in out
    assert "environment" in out


@pytest.mark.parametrize(
    "payload",
    [{"checks": {"keyring": {"status": "ok"}}}, {"checks": {"keyring": "bad"}}, {"checks": []}, []],
)
def test_doctor_handles_readiness_shapes_and_optional_identity(env, monkeypatch, payload):
    install(
        monkeypatch,
        lambda request: httpx.Response(
            200, json=payload if request.url.path == "/ready" else {"account_id": "acct"}
        ),
    )
    for token in ("", SECRET):
        code, out, _ = run(["doctor", "--json"], {**env, "LUCY_TOKEN": token})
        assert code in (0, 1)
        assert json.loads(out)["checks"]


def test_doctor_keeps_local_checks_when_the_network_fails(env, monkeypatch):
    def fail(request):
        raise httpx.ConnectError("private details", request=request)

    install(monkeypatch, fail)
    code, out, _ = run(["doctor", "--json"], env)
    assert code == 1
    assert len(json.loads(out)["checks"]) > 1
    assert "private details" not in out


def test_doctor_survives_a_non_json_proxy_reply(env, monkeypatch):
    install(monkeypatch, lambda request: httpx.Response(503, text="oops"))
    assert run(["doctor"], env)[0] == 1


def test_connect_lists_services_and_explains_incomplete_sign_in(env, monkeypatch):
    env["LUCY_TOKEN"] = SECRET
    install(monkeypatch, lambda request: httpx.Response(200, json={"services": [service()]}))
    assert run(["connect"], env)[0] == 0
    code, out, _ = run(["connect", "music"], env)
    assert code == 1
    assert "still needs a connected account" in out
    assert "cannot complete" not in out
    assert run(["connect", "unknown"], env)[0] == 2


def test_connect_can_report_a_ready_service_that_needs_no_connection(env, monkeypatch):
    env["LUCY_TOKEN"] = SECRET
    install(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"services": [service(connection_state="not_required")]}
        ),
    )
    code, out, _ = run(["connect", "music", "--json"], env)
    assert code == 0
    assert json.loads(out)["complete"]


def test_connect_treats_a_connected_account_as_done(env, monkeypatch):
    env["LUCY_TOKEN"] = SECRET
    install(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"services": [service(connection_state="connected")]}
        ),
    )
    code, out, _ = run(["connect", "music", "--json"], env)
    assert code == 0
    assert json.loads(out)["complete"]


def test_connect_displays_documentation_links_in_human_output(env, monkeypatch):
    row = service(
        actions=[{"description": "Read setup instructions.", "url": "https://docs.example/setup"}]
    )
    install(monkeypatch, lambda request: httpx.Response(200, json={"services": [row]}))
    code, out, _ = run(["connect", "music"], {**env, "LUCY_TOKEN": SECRET})
    assert code == 1
    assert "https://docs.example/setup" in out


@pytest.mark.parametrize("status", [401, 404, 503])
def test_connect_reports_recoverable_server_errors(env, monkeypatch, status):
    install(monkeypatch, lambda request: httpx.Response(status, text=SECRET))
    code, out, err = run(["connect"], {**env, "LUCY_TOKEN": SECRET})
    assert code == 1
    assert SECRET not in out + err


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"services": None},
        {"services": [None]},
        {"services": [{}]},
        {"services": [service(actions=None)]},
        {"services": [service(actions=[None])]},
        {"services": [service(actions=[{}])]},
    ],
)
def test_connect_refuses_malformed_catalogues_without_crashing(env, monkeypatch, body):
    install(monkeypatch, lambda request: httpx.Response(200, json=body))
    code, _, err = run(["connect"], {**env, "LUCY_TOKEN": SECRET})
    assert code == 1
    assert "unreadable" in err


def test_family_setup_without_a_desk_refuses_github_ci_install(env) -> None:
    code, out, _ = run(
        [
            "setup",
            "--mode",
            "family",
            "--url",
            "https://hub.example",
            "--no-token",
            "--json",
        ],
        env,
    )
    payload = json.loads(out)
    assert code != 0
    assert any("checkout" in notice.lower() for notice in payload["notices"])


def test_family_setup_reports_github_app_and_private_extras(env, tmp_path, monkeypatch) -> None:
    (tmp_path / "family-app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repos.txt").write_text(
        "Keyring-api https://github.com/example/Keyring-api.git\n", encoding="utf-8"
    )
    (tmp_path / ".repos.local.txt").write_text(
        "Archive-api https://example.invalid/Archive-api.git\n", encoding="utf-8"
    )
    (tmp_path / "Archive-api").mkdir()
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "genenv.local.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "lucy_api.cli.setup.github_app_state",
        lambda _root: {"done": False, "detail": "missing"},
    )
    code, out, _ = run(
        [
            "setup",
            "--mode",
            "family",
            "--url",
            "https://hub.example",
            "--no-token",
            "--no-github-ci",
            "--json",
        ],
        env,
    )
    rows = {row["id"]: row for row in json.loads(out)["already"]}
    assert code == 0
    assert rows["github_app"]["done"] is False
    assert "local checkouts" in rows["extras"]["detail"]
    assert "compose override" in rows["extras"]["detail"]
    assert "token extras" in rows["extras"]["detail"]


def test_an_already_installed_github_app_is_left_alone(env, tmp_path, monkeypatch) -> None:
    (tmp_path / "family-app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repos.txt").write_text("Keyring-api https://github.com/x/Keyring-api.git\n")
    monkeypatch.setattr(
        "lucy_api.cli.setup.github_app_state",
        lambda _root: {"done": True, "detail": "installed"},
    )
    code, out, _ = run(
        [
            "setup",
            "--mode",
            "family",
            "--url",
            "https://hub.example",
            "--no-token",
            "--json",
        ],
        env,
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["github_ci"]["already"] is True
    assert any("Already installed" in notice for notice in payload["notices"])


def test_a_github_ci_install_failure_is_reported_without_losing_setup(
    env, tmp_path, monkeypatch
) -> None:
    (tmp_path / "family-app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repos.txt").write_text("Keyring-api https://github.com/x/Keyring-api.git\n")
    monkeypatch.setattr(
        "lucy_api.cli.setup.github_app_state",
        lambda _root: {"done": False, "detail": "missing"},
    )

    def fail(*_args: object, **_kwargs: object) -> int:
        raise CliError("no gh", 1)

    monkeypatch.setattr("lucy_api.cli.setup.run_github_ci", fail)
    code, out, _ = run(
        [
            "setup",
            "--mode",
            "family",
            "--url",
            "https://hub.example",
            "--no-token",
            "--github-ci",
            "--json",
        ],
        env,
    )
    payload = json.loads(out)
    assert code == 1
    assert payload["saved"]
    assert "no gh" in payload["notices"]


def test_a_successful_github_ci_install_refreshes_status(env, tmp_path, monkeypatch) -> None:
    (tmp_path / "family-app.json").write_text("{}", encoding="utf-8")
    (tmp_path / "repos.txt").write_text("Keyring-api https://github.com/x/Keyring-api.git\n")
    states = iter(
        [
            {"done": False, "detail": "missing"},
            {"done": True, "detail": "installed"},
        ]
    )
    monkeypatch.setattr("lucy_api.cli.setup.github_app_state", lambda _root: next(states))
    monkeypatch.setattr("lucy_api.cli.setup.run_github_ci", lambda *_a, **_k: 0)
    code, out, _ = run(
        [
            "setup",
            "--mode",
            "family",
            "--url",
            "https://hub.example",
            "--no-token",
            "--github-ci",
            "--json",
        ],
        env,
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["github_ci"]["ok"] is True
    assert any(row["id"] == "github_app" and row["done"] for row in payload["already"])
