"""The family desk locator never invents a checkout or prints a GitHub token."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lucy_api.cli.base import FAMILY_ROOT_VAR, USAGE, CliError
from lucy_api.cli.family import (
    APP_PAGE,
    checkout_hint,
    extra_desk,
    find_family_root,
    github_app_state,
    github_ci_notice,
    is_family_root,
    load_connect,
    run_github_ci,
    should_install_github_app,
)

INSTALLER = """
class ConnectError(Exception):
    pass

STATE = {"fail_read": False, "found": False}

def read_family(path):
    if STATE["fail_read"]:
        raise ConnectError("bad manifest")
    return ("owner", ["Keyring-api"])

def read_app(path):
    return ("lucy-assistant-family-ci", {})

def run_tool(runner, args):
    return runner(list(args))

def find_installation(owner, slug, run=None):
    return STATE["found"]

def connect(**kwargs):
    return 4
"""


def _desk(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "family-app.json").write_text("{}", encoding="utf-8")
    (path / "repos.txt").write_text(
        "Keyring-api https://github.com/example/Keyring-api.git\n", encoding="utf-8"
    )
    return path


def _installer(desk: Path) -> Path:
    scripts = desk / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "connect_github.py").write_text(INSTALLER, encoding="utf-8")
    return desk


def test_an_explicit_family_root_wins_only_when_it_is_a_desk(tmp_path: Path) -> None:
    desk = _desk(tmp_path / "family")
    other = tmp_path / "other"
    other.mkdir()
    (other / "repos.txt").write_text("nope", encoding="utf-8")
    assert is_family_root(desk) is True
    assert find_family_root(cwd=other, environ={FAMILY_ROOT_VAR: str(desk)}) == desk.resolve()
    assert find_family_root(cwd=other, environ={FAMILY_ROOT_VAR: str(other)}) is None
    assert find_family_root(cwd=other, environ={FAMILY_ROOT_VAR: "  "}) is None


def test_a_walk_from_cwd_or_the_installed_tree_finds_the_desk(tmp_path: Path) -> None:
    desk = _desk(tmp_path / "family")
    nested = desk / "src" / "lucy_api"
    nested.mkdir(parents=True)
    assert find_family_root(cwd=nested, environ={}) == desk.resolve()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert find_family_root(cwd=elsewhere, environ={}, origin=desk) == desk.resolve()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_family_root(cwd=empty, environ={}, origin=empty) is None


def test_a_missing_installer_is_a_usage_error(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    with pytest.raises(CliError) as caught:
        load_connect(desk)
    assert caught.value.code == USAGE
    assert "connect_github.py" in str(caught.value)


def test_an_unreadable_installer_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = _installer(_desk(tmp_path))
    monkeypatch.setattr(
        "lucy_api.cli.family.importlib.util.spec_from_file_location", lambda *_a, **_k: None
    )
    with pytest.raises(CliError) as caught:
        load_connect(desk)
    assert caught.value.code == USAGE
    monkeypatch.setattr(
        "lucy_api.cli.family.importlib.util.spec_from_file_location",
        lambda *_a, **_k: SimpleNamespace(loader=None),
    )
    with pytest.raises(CliError):
        load_connect(desk)


def test_the_installer_loads_from_the_desk(tmp_path: Path) -> None:
    desk = _installer(_desk(tmp_path))
    module = load_connect(desk)
    assert module.connect() == 4


def test_github_ci_install_is_opt_in_except_in_family_mode() -> None:
    family = SimpleNamespace(args=Namespace(no_github_ci=False, github_ci=False))
    skipped = SimpleNamespace(args=Namespace(no_github_ci=True, github_ci=False))
    forced = SimpleNamespace(args=Namespace(no_github_ci=False, github_ci=True))
    assert should_install_github_app(family, "family") is True
    assert should_install_github_app(family, "hub") is False
    assert should_install_github_app(skipped, "family") is False
    assert should_install_github_app(forced, "hub") is True


def test_run_github_ci_hands_the_desk_paths_to_the_installer(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    seen: dict[str, object] = {}

    def connect(**kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    ctx = SimpleNamespace(
        args=Namespace(yes=True, dry_run=True, github_ci=False),
        interactive=True,
    )
    assert run_github_ci(ctx, desk, connect=connect) == 0
    assert seen["repos_file"] == desk / "repos.txt"
    assert seen["interactive"] is False
    assert seen["dry_run"] is True


def test_run_github_ci_loads_the_desk_installer_when_none_is_injected(tmp_path: Path) -> None:
    desk = _installer(_desk(tmp_path))
    ctx = SimpleNamespace(
        args=Namespace(yes=True, dry_run=False, github_ci=True),
        interactive=False,
    )
    assert run_github_ci(ctx, desk) == 4


def test_github_app_state_covers_each_install_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desk = _installer(_desk(tmp_path))
    module = load_connect(desk)
    monkeypatch.setattr("lucy_api.cli.family.load_connect", lambda _root: module)
    module.STATE["fail_read"] = True
    unknown = github_app_state(desk)
    assert unknown["state"] == "unknown"
    assert unknown["done"] is False

    module.STATE["fail_read"] = False
    signed_out = github_app_state(desk, run=lambda *_a, **_k: SimpleNamespace(returncode=1))
    assert signed_out["state"] == "signed_out"

    module.STATE["found"] = True
    installed = github_app_state(desk, run=lambda *_a, **_k: SimpleNamespace(returncode=0))
    assert installed["state"] == "installed"
    assert installed["done"] is True

    module.STATE["found"] = False
    missing = github_app_state(desk, run=lambda *_a, **_k: SimpleNamespace(returncode=0))
    assert missing["state"] == "missing"
    assert missing["account"] == "owner"


def test_extra_desk_reads_the_local_manifest_without_publishing_it(tmp_path: Path) -> None:
    desk = _desk(tmp_path)
    empty = extra_desk(desk)
    assert empty["manifest"] is False
    assert empty["checkouts"] == []
    (desk / "repos.local.txt").write_text(
        "# private extras\nArchive-api https://example.invalid/Archive-api.git\n",
        encoding="utf-8",
    )
    (desk / "Archive-api").mkdir()
    listed = extra_desk(desk)
    assert listed["manifest"] is True
    assert listed["checkouts"][0]["name"] == "Archive-api"
    assert listed["checkouts"][0]["present"] is True
    (desk / ".repos.local.txt").write_text(
        "Nested-api https://example.invalid/Nested-api.git\n", encoding="utf-8"
    )
    (desk / "private" / "Nested-api").mkdir(parents=True)
    nested = extra_desk(desk)
    assert nested["checkouts"][0]["name"] == "Nested-api"
    assert nested["checkouts"][0]["present"] is True
    (desk / ".repos.local.txt").write_text(
        "Missing-api https://example.invalid/Missing-api.git\n", encoding="utf-8"
    )
    absent = extra_desk(desk)["checkouts"][0]
    assert absent["present"] is False
    assert absent["path"] == ""
    assert extra_desk(desk)["compose_override"] is False


def test_github_ci_notices_name_the_page_and_never_a_token() -> None:
    assert "Already installed" in github_ci_notice(0, dry_run=False, already=True)
    assert "Would open" in github_ci_notice(0, dry_run=True)
    assert "Opened" in github_ci_notice(0, dry_run=False)
    assert "Could not complete" in github_ci_notice(1, dry_run=False)
    assert APP_PAGE in checkout_hint()
    assert FAMILY_ROOT_VAR in checkout_hint()
