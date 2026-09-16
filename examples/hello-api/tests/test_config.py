"""Configuration rules."""

from __future__ import annotations

from typing import Any

import pytest
from keyring_client.testing import ISSUER, JWKS_URL

from hello_api.core.config import LogFormat, Settings, check_for_unknown_env_vars, load_settings


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "log_format": LogFormat.CONSOLE,
        "keyring_issuer": ISSUER,
        "keyring_jwks_url": JWKS_URL,
        "audience": "hello",
    }
    return Settings(**{**defaults, **overrides})


def test_unknown_env_vars_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELLO_NOT_A_REAL_SETTING", "1")
    with pytest.raises(RuntimeError, match="HELLO_NOT_A_REAL_SETTING"):
        check_for_unknown_env_vars()


def test_bad_audience_refused() -> None:
    with pytest.raises(ValueError, match="HELLO_AUDIENCE"):
        _settings(audience="hello.work")


def test_load_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELLO_ENVIRONMENT", "test")
    monkeypatch.setenv("HELLO_LOG_FORMAT", "console")
    monkeypatch.setenv("HELLO_KEYRING_ISSUER", "https://keyring.test")
    monkeypatch.setenv("HELLO_KEYRING_JWKS_URL", "https://keyring.test/.well-known/jwks.json")
    monkeypatch.setenv("HELLO_AUDIENCE", "hello")
    for key in list(__import__("os").environ):
        if key.startswith("HELLO_") and key not in {
            "HELLO_ENVIRONMENT",
            "HELLO_LOG_FORMAT",
            "HELLO_KEYRING_ISSUER",
            "HELLO_KEYRING_JWKS_URL",
            "HELLO_AUDIENCE",
        }:
            monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert isinstance(settings, Settings)
    assert settings.environment == "test"
