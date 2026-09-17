"""Configuration rules: a typo is a startup error, and a bad audience is refused."""

from __future__ import annotations

import os
from typing import Any

import pytest

from lucy_api.core.config import (
    ENV_PREFIX,
    LogFormat,
    Settings,
    check_for_unknown_env_vars,
    load_settings,
)


def _settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {"_env_file": None, "log_format": LogFormat.CONSOLE}
    return Settings(**{**defaults, **overrides})


def test_an_unknown_prefixed_variable_is_a_startup_error() -> None:
    # The hub holds every sibling's base URL; a misspelled one must not start quietly.
    with pytest.raises(RuntimeError, match="LUCY_KEYRNIG_BASE_URL"):
        check_for_unknown_env_vars({"LUCY_KEYRNIG_BASE_URL": "http://127.0.0.1:8001"})


def test_a_known_variable_and_an_unprefixed_one_are_both_fine() -> None:
    check_for_unknown_env_vars({"LUCY_PORT": "8000", "PATH": "/usr/bin"})


@pytest.mark.parametrize("audience", ["", " lucy-api", "lucy-api "])
def test_an_unusable_audience_is_refused(audience: str) -> None:
    with pytest.raises(ValueError, match="LUCY_AUDIENCE"):
        _settings(audience=audience)


def test_every_field_is_reachable_through_the_prefix() -> None:
    # check_for_unknown_env_vars derives its allowlist from the model, so this is the
    # property that keeps the two in step rather than a hand-maintained list.
    known = {ENV_PREFIX + name.upper() for name in Settings.model_fields}
    check_for_unknown_env_vars(dict.fromkeys(known, "x"))


def test_load_settings_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LUCY_ENVIRONMENT", "test")
    monkeypatch.setenv("LUCY_LOG_FORMAT", "console")
    settings = load_settings()
    assert settings.environment == "test"
    assert settings.log_format is LogFormat.CONSOLE
    assert settings.port == 8000
