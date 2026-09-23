"""Configuration rules: a typo is a startup error, and a bad audience is refused."""

from __future__ import annotations

import os
from typing import Any

import pytest

from lucy_api.model.wire import DEFAULT_TIMEOUT

from lucy_api.core.config import (
    CLIENT_VARIABLES,
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


def test_load_settings_reads_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LUCY_ENVIRONMENT", "test")
    monkeypatch.setenv("LUCY_LOG_FORMAT", "console")
    settings = load_settings()
    assert settings.environment == "test"
    assert settings.log_format is LogFormat.CONSOLE
    assert settings.port == 8000


def test_server_accepts_exactly_the_clients_documented_prefixed_variables() -> None:
    from lucy_api.cli.base import FAMILY_ROOT_VAR, TOKEN_VAR, URL_VAR
    from lucy_api.cli.config import CONFIG_VAR

    assert {URL_VAR, TOKEN_VAR, CONFIG_VAR, FAMILY_ROOT_VAR} == CLIENT_VARIABLES
    check_for_unknown_env_vars(dict.fromkeys(CLIENT_VARIABLES, "client-only-value"))
    with pytest.raises(RuntimeError, match="LUCY_TOKNE"):
        check_for_unknown_env_vars({"LUCY_TOKNE": "not-printed-secret"})


def test_validation_errors_hide_a_bad_setting_value() -> None:
    with pytest.raises(ValueError, match="port") as raised:
        _settings(port="a-secret-accidentally-pasted-here")
    assert "a-secret-accidentally-pasted-here" not in str(raised.value)


def test_an_extra_sibling_is_ignored_until_it_has_a_base_url() -> None:
    from lucy_api.core.config import ExtraSibling

    settings = _settings(
        extra_services={
            "archive": ExtraSibling(base_url="http://127.0.0.1:8010", audience="archive"),
            "blank": ExtraSibling(base_url="   "),
        }
    )
    lookup = settings.extra
    archive = lookup("archive")
    assert archive is not None
    assert archive.audience == "archive"
    assert lookup("blank") is None
    assert lookup("missing") is None


def test_a_model_is_given_longer_than_a_sibling_service() -> None:
    """A sibling that has not replied in ten seconds is broken; a model is thinking. The two
    shared one setting, and the first real turn ever served here died as `the model was
    unavailable (ReadTimeout)` after ten seconds of a reply that arrived, whole and correct,
    at twenty-six."""
    settings = _settings()
    assert settings.model_timeout_seconds > settings.http_timeout_seconds
    assert settings.model_timeout_seconds == DEFAULT_TIMEOUT


def test_the_model_timeout_is_its_own_knob() -> None:
    settings = _settings(model_timeout_seconds=300.0)
    assert settings.model_timeout_seconds == 300.0
    assert settings.http_timeout_seconds == 10.0
