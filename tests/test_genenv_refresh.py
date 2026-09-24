"""Refreshing `.env.family` keeps what must not change, and says what it did not keep.

On 2026-09-23 the family's environment file was stale -- it predated two services -- and the
only way to regenerate it was `--force`, which also rotated `KEYRING_MASTER_KEY`: the key that
decrypts every credential already stored in keyring. The fix that day was to rotate
everything and put the old master key back by hand. And the hand edits that point the hub at
a model runtime on the host (`LUCY_MODEL_BASE_URLS`) would have vanished on the next refresh,
with nothing said.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import genenv  # noqa: E402

CLYDE = '{"clyde":"http://host.docker.internal:8127/v1"}'


def _file(tmp_path: Path, **values: str) -> Path:
    path = tmp_path / ".env.family"
    lines = [
        "# written by hand for this test",
        "",
        *(f"{key}={value}" for key, value in values.items()),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _extras(tmp_path: Path, body: object) -> Path:
    path = tmp_path / "genenv.local.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _values(path: Path) -> dict[str, str]:
    return genenv.read_existing(path)


def test_a_refresh_keeps_the_master_key_so_stored_credentials_still_decrypt(
    tmp_path: Path,
) -> None:
    """The bug, named: `--force` used to rotate the one secret that must not rotate."""
    original = genenv.new_master_key()
    path = _file(tmp_path, KEYRING_MASTER_KEY=original, KEYRING_ADMIN_TOKEN="old-admin")

    written = genenv.write_env(path, force=True, extras_path=None)

    assert written.kept_master_key is True
    assert _values(path)["KEYRING_MASTER_KEY"] == original
    assert _values(path)["KEYRING_ADMIN_TOKEN"] != "old-admin"


def test_rotating_the_master_key_is_its_own_explicit_choice(tmp_path: Path) -> None:
    original = genenv.new_master_key()
    path = _file(tmp_path, KEYRING_MASTER_KEY=original)

    written = genenv.write_env(path, force=True, rotate_master_key=True, extras_path=None)

    assert written.kept_master_key is False
    assert _values(path)["KEYRING_MASTER_KEY"] != original


def test_a_file_without_a_master_key_gets_a_new_one(tmp_path: Path) -> None:
    path = _file(tmp_path, SOMETHING_ELSE="1")
    written = genenv.write_env(path, force=True, extras_path=None)
    assert written.kept_master_key is False
    assert _values(path)["KEYRING_MASTER_KEY"]


def test_a_first_write_has_nothing_to_keep_or_drop(tmp_path: Path) -> None:
    written = genenv.write_env(tmp_path / ".env.family", force=False, extras_path=None)
    assert written.kept_master_key is False
    assert written.dropped == ()


def test_a_refresh_names_the_hand_edits_it_did_not_carry_over(tmp_path: Path) -> None:
    path = _file(tmp_path, KEYRING_MASTER_KEY=genenv.new_master_key(), LUCY_MODEL_BASE_URLS=CLYDE)
    written = genenv.write_env(path, force=True, extras_path=None)
    assert written.dropped == ("LUCY_MODEL_BASE_URLS",)
    assert "LUCY_MODEL_BASE_URLS" not in _values(path)


def test_this_machines_own_variables_survive_a_refresh(tmp_path: Path) -> None:
    extras = _extras(tmp_path, {"env": {"LUCY_MODEL_BASE_URLS": CLYDE}})
    path = _file(tmp_path, KEYRING_MASTER_KEY=genenv.new_master_key(), LUCY_MODEL_BASE_URLS=CLYDE)

    written = genenv.write_env(path, force=True, extras_path=extras)

    assert written.dropped == ()
    assert _values(path)["LUCY_MODEL_BASE_URLS"] == CLYDE


def test_an_operator_variable_can_never_replace_a_generated_secret(tmp_path: Path) -> None:
    extras = _extras(tmp_path, {"env": {"KEYRING_ADMIN_TOKEN": "chosen-by-hand"}})
    with pytest.raises(genenv.ExtraConfigError, match="written by the generator"):
        genenv.build_env(extras)


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        ({"env": ["LUCY_MODEL_BASE_URLS"]}, "must be an object"),
        ({"env": {"lucy_model_base_urls": "x"}}, "UPPER_SNAKE"),
        ({"env": {"9LIVES": "x"}}, "UPPER_SNAKE"),
        ({"env": {"LUCY_MODEL_BASE_URLS": {"clyde": "x"}}}, "must be a string"),
    ],
)
def test_an_operator_variable_has_to_look_like_one(
    tmp_path: Path, body: object, complaint: str
) -> None:
    with pytest.raises(genenv.ExtraConfigError, match=complaint):
        genenv.load_local_env(_extras(tmp_path, body))


def test_an_extras_file_that_is_not_json_is_refused_by_name(tmp_path: Path) -> None:
    extras = tmp_path / "genenv.local.json"
    extras.write_text("{not json", encoding="utf-8")
    with pytest.raises(genenv.ExtraConfigError, match="not valid JSON"):
        genenv.load_local_env(extras)


def test_no_extras_file_and_no_env_section_both_mean_no_variables(tmp_path: Path) -> None:
    assert genenv.load_local_env(None) == {}
    assert genenv.load_local_env(tmp_path / "missing.json") == {}
    assert genenv.load_local_env(_extras(tmp_path, {"keyring_consumers": []})) == {}


def test_reading_an_old_file_skips_comments_blanks_and_stray_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env.family"
    path.write_text("# comment\n\nNOT A VARIABLE\n  # indented\nA=1\nB=x=y\n", encoding="utf-8")
    assert genenv.read_existing(path) == {"A": "1", "B": "x=y"}
    assert genenv.read_existing(tmp_path / "absent") == {}


def test_the_command_says_it_kept_the_key_and_what_it_dropped_but_never_a_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(genenv, "LOCAL_EXTRAS", tmp_path / "none.json")
    original = genenv.new_master_key()
    path = _file(tmp_path, KEYRING_MASTER_KEY=original, LUCY_MODEL_BASE_URLS=CLYDE)
    monkeypatch.setattr(genenv, "write_env", _without_extras(genenv.write_env))

    code = genenv.main(["--force", "--output", str(path)])

    out = capsys.readouterr().out
    assert code == 0
    assert "Kept KEYRING_MASTER_KEY" in out
    assert "Not carried over: LUCY_MODEL_BASE_URLS" in out
    assert original not in out
    assert CLYDE not in out
    for value in _values(path).values():
        assert value not in out


def test_the_command_rotates_the_key_only_when_told_to(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = genenv.new_master_key()
    path = _file(tmp_path, KEYRING_MASTER_KEY=original)
    monkeypatch.setattr(genenv, "write_env", _without_extras(genenv.write_env))

    code = genenv.main(["--force", "--rotate-master-key", "--output", str(path)])

    assert code == 0
    assert "Kept KEYRING_MASTER_KEY" not in capsys.readouterr().out
    assert _values(path)["KEYRING_MASTER_KEY"] != original


def _without_extras(write_env: object) -> object:
    """`write_env` pinned to no extras file, so a real `genenv.local.json` cannot leak in."""

    def pinned(path: Path, **kwargs: object) -> object:
        return write_env(path, extras_path=None, **kwargs)  # type: ignore[operator]

    return pinned
