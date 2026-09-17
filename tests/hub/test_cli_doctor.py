"""Diagnostics report independent failures without blaming an outage on credentials."""

from __future__ import annotations

import httpx
import pytest

from lucy_api.cli import doctor
from lucy_api.cli.config import Config


def test_environment_checks_report_installed_tools_configuration_and_redacted_identity(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor.sys, "version_info", (3, 12, 8))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    saved = Config(tmp_path / "config.toml", {"token": "header.payload.signature"}, (), True)
    checks = doctor.environment_checks(config=saved, environ={}, token=saved.get("token"))
    assert [check.name for check in checks] == ["python", "lucy", "uv", "docker", "config", "token"]
    assert doctor.worst(checks) == doctor.OK
    assert all(not check.failed for check in checks)
    assert checks[0].detail == "3.12.8"
    assert checks[-1].detail == "...ture"
    assert saved.get("token") not in repr(checks)


def test_missing_dependencies_are_independent_and_each_names_its_fix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(doctor.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    checks = doctor.environment_checks(
        config=Config(tmp_path / "absent.toml", {}, (), False), environ={}, token=""
    )
    assert len(checks) == 6
    assert checks[0].failed
    assert "3.12" in checks[0].fix
    assert all(check.fix for check in checks)
    assert checks[-1].detail == "not signed in"
    assert doctor.worst(checks) == doctor.FAIL


def test_config_warnings_name_overrides_and_unknown_keys_without_echoing_values(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    saved = Config(
        tmp_path / "config.toml",
        {"url": "https://saved.example", "token": "saved-secret"},
        ("uri", "typo"),
        True,
    )
    checks = doctor.environment_checks(
        config=saved,
        environ={"LUCY_URL": "https://env.example", "LUCY_TOKEN": "env-secret"},
        token="env-secret",
    )
    by_name = {check.name: check for check in checks}
    assert by_name["config keys"].level == doctor.WARN
    assert "uri, typo" in by_name["config keys"].detail
    for name in ("LUCY_URL", "LUCY_TOKEN"):
        assert "wins over" in by_name[name].detail
        assert f"unset {name}" in by_name[name].fix
    assert by_name["token"].level == doctor.WARN
    assert "whole thing" in by_name["token"].fix
    for secret in ("saved-secret", "env-secret", "https://saved.example", "https://env.example"):
        assert secret not in repr(checks)


@pytest.mark.parametrize(
    ("values", "environ"),
    [
        ({}, {"LUCY_URL": "https://hub.example", "LUCY_TOKEN": "env-token"}),
        (
            {"url": "https://hub.example", "token": "saved-token"},
            {"LUCY_URL": " ", "LUCY_TOKEN": " "},
        ),
    ],
)
def test_environment_does_not_warn_about_an_override_when_only_one_source_is_set(
    tmp_path, monkeypatch, values, environ
) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    checks = doctor.environment_checks(
        config=Config(tmp_path / "config.toml", values, (), True), environ=environ, token=""
    )
    assert not {"LUCY_URL", "LUCY_TOKEN"}.intersection(check.name for check in checks)


@pytest.mark.parametrize("error", [None, "cannot reach the hub"])
def test_unreachable_hub_has_one_failure_with_a_fix(error) -> None:
    checks = doctor.hub_checks("https://hub.example", None, error)
    assert len(checks) == 1
    assert checks[0].failed
    assert checks[0].detail == (error or "cannot reach https://hub.example")
    assert "lucy serve" in checks[0].fix


def test_healthy_hub_and_identity_are_independent_successes() -> None:
    checks = doctor.hub_checks(
        "https://hub.example",
        {
            "ready": httpx.Response(200),
            "checks": {"zebra": "ok", "alpha": "ok"},
            "me": httpx.Response(200, json={"account_id": "account-a"}),
        },
        None,
    )
    assert [check.name for check in checks] == ["hub", "ready", "  alpha", "  zebra", "identity"]
    assert checks[-1].detail == "account-a"
    assert doctor.worst(checks) == doctor.OK


@pytest.mark.parametrize("ready", [None, httpx.Response(503)])
def test_failed_readiness_keeps_dependency_details_and_unsigned_identity(ready) -> None:
    checks = doctor.hub_checks(
        "https://hub.example", {"ready": ready, "checks": {"keyring": "degraded"}}, None
    )
    assert checks[1].failed
    assert checks[2].failed
    assert checks[2].fix == "start keyring"
    assert checks[-1].level == doctor.WARN
    assert checks[-1].detail == "not checked without a token"


@pytest.mark.parametrize(
    ("status", "level", "detail", "fix"),
    [
        (401, doctor.FAIL, "refused the token", "lucy setup"),
        (403, doctor.FAIL, "denied access", "permissions"),
        (429, doctor.WARN, "rate limiting", "try again later"),
        (500, doctor.FAIL, "unavailable", "same token"),
        (503, doctor.FAIL, "unavailable", "same token"),
        (404, doctor.FAIL, "unexpected status", "LUCY_URL"),
        (302, doctor.FAIL, "unexpected status", "LUCY_URL"),
    ],
)
def test_identity_failure_reports_the_actual_remedy_without_echoing_a_server_body(
    status, level, detail, fix
) -> None:
    checks = doctor.hub_checks(
        "https://hub.example",
        {"ready": httpx.Response(200), "me": httpx.Response(status, text="echoed-secret")},
        None,
    )
    identity = checks[-1]
    assert identity.level == level
    assert detail in identity.detail
    assert fix in identity.fix
    assert "echoed-secret" not in repr(checks)
    if status != 401:
        assert "expired" not in identity.fix
        assert "lucy setup" not in identity.fix


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="secret-not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"account_id": ["secret"]}),
    ],
)
def test_malformed_identity_response_is_a_reported_failure_not_a_traceback(response) -> None:
    checks = doctor.hub_checks("https://hub.example", {"me": response}, None)
    assert checks[-1].failed
    assert "invalid identity response" in checks[-1].detail
    assert "secret" not in repr(checks)


@pytest.mark.parametrize("states", [None, [], "server-secret"])
def test_malformed_dependency_checks_do_not_break_the_remaining_report(states) -> None:
    checks = doctor.hub_checks("https://hub.example", {"checks": states}, None)
    assert checks[-2].failed
    assert checks[-2].name == "dependencies"
    assert checks[-1].name == "identity"
    assert "server-secret" not in repr(checks)


def test_worst_handles_no_checks_success_warnings_and_failures() -> None:
    assert doctor.worst([]) == doctor.OK
    assert doctor.worst([doctor.Check("a", doctor.OK, "fine")]) == doctor.OK
    assert doctor.worst([doctor.Check("a", doctor.WARN, "optional")]) == doctor.WARN
    assert (
        doctor.worst(
            [doctor.Check("a", doctor.WARN, "optional"), doctor.Check("b", doctor.FAIL, "broken")]
        )
        == doctor.FAIL
    )
