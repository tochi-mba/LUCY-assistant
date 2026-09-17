"""Configuration is deterministic, atomic, private, and safe to diagnose."""

from __future__ import annotations

import errno
import os
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from lucy_api.cli import config
from lucy_api.cli.base import resolve_token, resolve_url


@pytest.mark.parametrize(
    ("platform", "environ", "relative"),
    [
        ("nt", {"LUCY_CONFIG": " chosen.toml "}, "chosen.toml"),
        ("nt", {"XDG_CONFIG_HOME": " xdg ", "APPDATA": "roaming"}, "xdg/lucy/config.toml"),
        ("posix", {"XDG_CONFIG_HOME": "xdg"}, "xdg/lucy/config.toml"),
        ("nt", {"APPDATA": " roaming "}, "roaming/lucy/config.toml"),
        ("posix", {"APPDATA": "ignored"}, "home/.config/lucy/config.toml"),
        ("nt", {"LUCY_CONFIG": " ", "APPDATA": " "}, "home/.config/lucy/config.toml"),
    ],
)
def test_configuration_path_honours_explicit_and_platform_directories(
    monkeypatch, platform, environ, relative
) -> None:
    monkeypatch.setattr(config, "os", SimpleNamespace(name=platform))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("home")))
    assert config.config_path(environ) == Path(relative)


def test_a_missing_home_requires_an_explicit_private_path(monkeypatch, tmp_path) -> None:
    def unavailable(cls):
        raise RuntimeError("no home")

    monkeypatch.setattr(Path, "home", classmethod(unavailable))
    with pytest.raises(config.ConfigError, match="set LUCY_CONFIG"):
        config.config_path({})
    assert config.config_path({"LUCY_CONFIG": str(tmp_path / "private.toml")}) == (
        tmp_path / "private.toml"
    )


def test_missing_config_is_an_empty_explicit_state(tmp_path) -> None:
    path = tmp_path / "missing.toml"
    loaded = config.load_config({"LUCY_CONFIG": str(path)})
    assert loaded == config.Config(path, {}, (), False)
    assert loaded.get("url") == ""


def test_load_keeps_known_strings_and_reports_unknown_keys_in_order(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'url = "https://hub.example"\nmode = "remote"\nzebra = true\nalpha = "typo"\n',
        encoding="utf-8",
    )
    loaded = config.load_config({"LUCY_CONFIG": str(path)})
    assert loaded.exists
    assert loaded.values == {"url": "https://hub.example", "mode": "remote"}
    assert loaded.unknown == ("alpha", "zebra")
    assert loaded.get("mode") == "remote"


@pytest.mark.parametrize("raw", [b'token = "unclosed-secret', b'token = "secret\xff"'])
def test_malformed_config_never_echoes_credential_text(tmp_path, raw) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(raw)
    with pytest.raises(config.ConfigError, match="not valid UTF-8 TOML") as raised:
        config.load_config({"LUCY_CONFIG": str(path)})
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("value", ["7", "false", "['secret']", "{value='secret'}"])
def test_non_string_settings_name_the_key_without_its_value(tmp_path, value) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f"token = {value}", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="token must be a string in quotes") as raised:
        config.load_config({"LUCY_CONFIG": str(path)})
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "failure", [OSError(errno.EACCES, "permission denied"), OSError("unreadable")]
)
def test_read_failure_is_actionable(monkeypatch, tmp_path, failure) -> None:
    def read_bytes(self):
        raise failure

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(config.ConfigError, match="cannot read"):
        config.load_config({"LUCY_CONFIG": str(tmp_path / "config.toml")})


def test_render_round_trips_unicode_quotes_and_backslashes_without_extra_keys() -> None:
    values = {
        "mode": "remote",
        "token": 'made-up-"credential"\\value',
        "url": "https://café.example",
    }
    rendered = config.render({**values, "unknown": "omitted"})
    assert tomllib.loads(rendered) == values
    assert rendered.index("url =") < rendered.index("token =") < rendered.index("mode =")
    assert "unknown" not in rendered
    assert tomllib.loads(config.render({"token": ""})) == {}


@pytest.mark.parametrize("control", ["\n", "\r", "\t", "\x00", "\x1f", "\x7f"])
def test_control_characters_are_refused_before_a_file_is_created(tmp_path, control) -> None:
    path = tmp_path / "new" / "config.toml"
    with pytest.raises(config.ConfigError, match="token must not contain control") as raised:
        config.save_config({"token": f"secret{control}"}, {"LUCY_CONFIG": str(path)})
    assert "secret" not in str(raised.value)
    assert not path.parent.exists()


def test_saving_atomically_replaces_existing_configuration_and_ignores_stale_temp_files(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "private" / "config.toml"
    environ = {"LUCY_CONFIG": str(path)}
    assert config.save_config({"url": "https://old.example", "token": "old-token"}, environ) == path
    stale = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    stale.write_text("unrelated file", encoding="utf-8")
    original_replace = Path.replace
    observed = []

    def replace(self, destination):
        observed.append((self, config.load_config(environ).get("token")))
        assert self.parent == path.parent
        assert self != stale
        assert tomllib.loads(self.read_text(encoding="utf-8"))["token"] == "new-token"
        return original_replace(self, destination)

    monkeypatch.setattr(Path, "replace", replace)
    config.save_config({"url": "https://new.example", "token": "new-token"}, environ)
    assert observed[0][1] == "old-token"
    assert config.load_config(environ).get("token") == "new-token"
    assert stale.read_text(encoding="utf-8") == "unrelated file"
    assert set(path.parent.iterdir()) == {path, stale}
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == config.OWNER_ONLY
        assert stat.S_IMODE(path.parent.stat().st_mode) == config.DIRECTORY_OWNER_ONLY


def test_save_failure_before_temp_creation_leaves_existing_file_untouched(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('token = "old-token"', encoding="utf-8")

    def cannot_create(*args, **kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(config.tempfile, "NamedTemporaryFile", cannot_create)
    with pytest.raises(config.ConfigError, match="cannot write"):
        config.save_config({"token": "new-token"}, {"LUCY_CONFIG": str(path)})
    assert tomllib.loads(path.read_text(encoding="utf-8"))["token"] == "old-token"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_failed_replace_keeps_old_file_and_attempts_temporary_cleanup(
    tmp_path, monkeypatch, fail_cleanup
) -> None:
    path = tmp_path / "config.toml"
    path.write_text('token = "old-token"', encoding="utf-8")
    attempted = []
    original_unlink = Path.unlink

    def cannot_replace(self, target):
        raise OSError("replacement unavailable")

    def unlink(self, *, missing_ok=False):
        attempted.append(self)
        if fail_cleanup:
            raise OSError("cleanup unavailable")
        return original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "replace", cannot_replace)
    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(config.ConfigError, match="replacement unavailable"):
        config.save_config({"token": "new-token"}, {"LUCY_CONFIG": str(path)})
    assert len(attempted) == 1
    assert attempted[0].exists() is fail_cleanup
    assert tomllib.loads(path.read_text(encoding="utf-8"))["token"] == "old-token"


@pytest.mark.parametrize(
    ("token", "expected"), [("", ""), ("12345678", "..."), ("123456789", "...6789")]
)
def test_redaction_never_discloses_a_short_token(token, expected) -> None:
    assert config.redact(token) == expected


def test_config_representation_never_includes_saved_credentials(tmp_path) -> None:
    saved = config.Config(tmp_path / "config.toml", {"token": "secret-value"}, (), True)
    assert "secret-value" not in repr(saved)
    assert "secret-value" not in str(saved)
    assert "<redacted>" in repr(saved)


@pytest.mark.parametrize(
    ("flag", "environment", "expected"),
    [
        ("https://flag.example", {"LUCY_URL": "https://env.example"}, "https://flag.example"),
        (None, {"LUCY_URL": " https://env.example "}, "https://env.example"),
        (None, {"LUCY_URL": " "}, "https://file.example"),
    ],
)
def test_saved_url_has_lower_precedence_than_flags_and_environment(
    tmp_path, flag, environment, expected
) -> None:
    saved = config.Config(tmp_path / "config.toml", {"url": "https://file.example"}, (), True)
    assert resolve_url(flag, environment, saved) == expected


def test_environment_token_wins_over_saved_token_without_reading_a_real_config(tmp_path) -> None:
    saved = config.Config(tmp_path / "config.toml", {"token": "saved-token"}, (), True)
    assert resolve_token({"LUCY_TOKEN": " env-token "}, saved) == "env-token"
    assert resolve_token({"LUCY_TOKEN": " "}, saved) == "saved-token"
    assert resolve_token({}, saved) == "saved-token"
    assert resolve_token({}) == ""
