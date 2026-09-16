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
        if key in {"KEYRING_SERVICE_TOKENS", "SETTINGS_API_SERVICES"}:
            continue
        assert len(value) >= genenv.MIN_TOKEN_CHARS, key


def test_keyring_json_matches_consumer_variables() -> None:
    env = genenv.build_env()
    mapping = json.loads(env["KEYRING_SERVICE_TOKENS"])
    for name, variable in genenv.KEYRING_CONSUMERS:
        assert mapping[name] == env[variable]
        assert len(mapping[name]) >= genenv.MIN_TOKEN_CHARS


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
    assert keyring.isdisjoint(settings)


def test_refuse_overwrite_without_force(tmp_path: Path) -> None:
    path = tmp_path / ".env.family"
    path.write_text("already=1\n", encoding="utf-8")
    with pytest.raises(genenv.AlreadyExistsError, match="already exists"):
        genenv.write_env(path, force=False)
    assert path.read_text(encoding="utf-8") == "already=1\n"


def test_force_replaces(tmp_path: Path) -> None:
    path = tmp_path / ".env.family"
    path.write_text("already=1\n", encoding="utf-8")
    count = genenv.write_env(path, force=True)
    text = path.read_text(encoding="utf-8")
    assert count > 0
    assert "KEYRING_SERVICE_TOKENS=" in text
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


def test_main_does_not_print_secret_on_refuse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
