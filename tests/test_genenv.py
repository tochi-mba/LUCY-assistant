"""scripts/genenv.py writes tokens and never prints them."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import genenv  # noqa: E402


def test_build_env_tokens_meet_the_floor() -> None:
    env = genenv.build_env()
    for key, value in env.items():
        if key == "KEYRING_MASTER_KEY":
            continue
        if key in {
            "KEYRING_SERVICE_TOKENS",
            "KEYRING_EXCHANGE_AUDIENCES",
            "SETTINGS_API_SERVICES",
            "MEMORY_SERVICE_TOKENS",
        }:
            continue
        assert len(value) >= genenv.MIN_TOKEN_CHARS, key


def test_keyring_json_matches_consumer_variables() -> None:
    env = genenv.build_env()
    mapping = json.loads(env["KEYRING_SERVICE_TOKENS"])
    for name, variable in genenv.KEYRING_CONSUMERS:
        assert mapping[name] == env[variable]
        assert len(mapping[name]) >= genenv.MIN_TOKEN_CHARS


def test_lucy_exchange_audiences_are_an_explicit_complete_allowlist() -> None:
    env = genenv.build_env()
    allowlists = json.loads(env["KEYRING_EXCHANGE_AUDIENCES"])

    assert allowlists == {"lucy-api": list(genenv.LUCY_EXCHANGE_AUDIENCES)}
    assert {
        # `persona`, not `persona-api`. The service's name is not the audience it pins, and
        # this test asserted the name for as long as the generator wrote it.
        "persona",
        "memory-api",
        "environments-api",
        "web-search-api",
        "spotify-api",
        "settings",
    } <= set(allowlists["lucy-api"])


def test_settings_services_match_settings_api_shape() -> None:
    env = genenv.build_env()
    grants = json.loads(env["SETTINGS_API_SERVICES"])
    for name, audience_prefix, namespaces, token_var in genenv.SETTINGS_GRANTS:
        row = grants[name]
        assert set(row) == {"token", "audience_prefix", "namespaces"}
        assert row["audience_prefix"] == audience_prefix
        assert row["namespaces"] == list(namespaces)
        assert len(row["token"]) >= genenv.MIN_TOKEN_CHARS
        if token_var is not None:
            assert env[token_var] == row["token"]


def test_settings_and_keyring_tokens_are_not_shared() -> None:
    env = genenv.build_env()
    keyring = set(json.loads(env["KEYRING_SERVICE_TOKENS"]).values())
    settings = {row["token"] for row in json.loads(env["SETTINGS_API_SERVICES"]).values()}
    memory = set(json.loads(env["MEMORY_SERVICE_TOKENS"]).values())
    assert keyring.isdisjoint(settings)
    assert keyring.isdisjoint(memory)
    assert settings.isdisjoint(memory)


def test_memory_service_token_is_lucy_s_internal_credential() -> None:
    env = genenv.build_env()
    mapping = json.loads(env["MEMORY_SERVICE_TOKENS"])
    assert mapping == {"lucy-api": env["LUCY_MEMORY_API_TOKEN"]}
    assert len(env["LUCY_MEMORY_API_TOKEN"]) >= genenv.MIN_TOKEN_CHARS


def test_refuse_overwrite_without_force(tmp_path: Path) -> None:
    path = tmp_path / ".env.family"
    path.write_text("already=1\n", encoding="utf-8")
    with pytest.raises(genenv.AlreadyExistsError, match="already exists"):
        genenv.write_env(path, force=False)
    assert path.read_text(encoding="utf-8") == "already=1\n"


def test_force_replaces(tmp_path: Path) -> None:
    path = tmp_path / ".env.family"
    path.write_text("already=1\n", encoding="utf-8")
    written = genenv.write_env(path, force=True)
    text = path.read_text(encoding="utf-8")
    assert written.count > 0
    assert "KEYRING_SERVICE_TOKENS=" in text
    assert "KEYRING_EXCHANGE_AUDIENCES=" in text
    assert "SETTINGS_API_SERVICES=" in text
    assert "already=1" not in text


def test_main_stdout_contains_no_secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / ".env.family"
    code = genenv.main(["--output", str(path)])
    assert code == 0
    stdout = capsys.readouterr().out
    written = path.read_text(encoding="utf-8")
    secrets = []
    for line in written.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        secrets.append(line.split("=", 1)[1])
    for value in secrets:
        assert value not in stdout
    assert "Wrote" in stdout
    assert str(path.name) in stdout


def test_main_does_not_print_secret_on_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / ".env.family"
    path.write_text("SECRETVALUE_should_not_leak=1\n", encoding="utf-8")
    code = genenv.main(["--output", str(path)])
    assert code == 1
    captured = capsys.readouterr()
    assert "SECRETVALUE_should_not_leak" not in captured.out
    assert "SECRETVALUE_should_not_leak" not in captured.err


def test_master_key_is_32_decoded_bytes() -> None:
    import base64

    env = genenv.build_env()
    raw = base64.b64decode(env["KEYRING_MASTER_KEY"], validate=True)
    assert len(raw) == genenv.MASTER_KEY_BYTES


def test_generated_environment_never_contains_github_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "FAMILY_GITHUB_TOKEN"):
        monkeypatch.setenv(name, "test-only-github-secret")
    rendered = genenv.render(genenv.build_env())
    assert "GITHUB" not in rendered
    assert "GH_TOKEN" not in rendered
    assert "test-only-github-secret" not in rendered


# --- the local extras file refuses anything it cannot use, and says which part ---------------


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        ('{"keyring_consumers": {}}', "keyring_consumers must be a list"),
        ('{"keyring_consumers": [["only-one"]]}', r"\[name, ENV_VAR\]"),
        ('{"keyring_consumers": [["", "VAR"]]}', "two non-empty strings"),
        ('{"settings_grants": {}}', "settings_grants must be a list"),
        ('{"settings_grants": [["a", "b", []]]}', "token_var_or_null"),
        ('{"settings_grants": [["", "b", [], null]]}', "name and audience_prefix"),
        ('{"settings_grants": [["a", "b", "ns", null]]}', "list of strings"),
        ('{"settings_grants": [["a", "b", ["ns"], ""]]}', "non-empty string or null"),
    ],
)
def test_an_unusable_extras_file_is_refused_by_name(
    tmp_path: Path, body: str, complaint: str
) -> None:
    extras = tmp_path / "genenv.local.json"
    extras.write_text(body, encoding="utf-8")
    with pytest.raises(genenv.ExtraConfigError, match=complaint):
        genenv.load_local_extras(extras)


def test_extras_are_added_to_the_published_lists(tmp_path: Path) -> None:
    extras = tmp_path / "genenv.local.json"
    extras.write_text(
        json.dumps(
            {
                "keyring_consumers": [["private-api", "PRIVATE_KEYRING_TOKEN"]],
                "settings_grants": [["private-api", "private-api", ["private"], None]],
            }
        ),
        encoding="utf-8",
    )
    env = genenv.build_env(extras)
    assert "private-api" in json.loads(env["KEYRING_SERVICE_TOKENS"])
    assert "private-api" in json.loads(env["SETTINGS_API_SERVICES"])
    assert env["PRIVATE_KEYRING_TOKEN"]


@pytest.mark.parametrize(
    "body",
    [
        {"keyring_consumers": [["lucy-api", "OTHER"]]},
        {"settings_grants": [["lucy-api", "lucy-api", ["lucy"], None]]},
    ],
)
def test_an_extra_cannot_shadow_a_published_service(tmp_path: Path, body: object) -> None:
    extras = tmp_path / "genenv.local.json"
    extras.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(genenv.ExtraConfigError, match="duplicates a published service"):
        genenv.build_env(extras)


def test_a_short_token_is_refused_rather_than_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(genenv.secrets, "token_urlsafe", lambda _n: "short")
    with pytest.raises(RuntimeError, match="shorter than the floor"):
        genenv.new_token()


def test_main_reports_an_unusable_extras_file_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    extras = tmp_path / "genenv.local.json"
    extras.write_text("{not json", encoding="utf-8")
    original = genenv.write_env
    monkeypatch.setattr(
        genenv, "write_env", lambda path, **kwargs: original(path, extras_path=extras, **kwargs)
    )
    path = tmp_path / ".env.family"
    assert genenv.main(["--output", str(path)]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert not path.exists()


def test_main_reports_a_file_it_cannot_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_path: Path, **_kwargs: object) -> object:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(genenv, "write_env", refuse)
    assert genenv.main(["--output", str(tmp_path / ".env.family")]) == 1
    assert "cannot write .env.family: Permission denied" in capsys.readouterr().err
