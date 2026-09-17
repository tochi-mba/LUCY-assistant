"""connect_github.py opens the public app Install page; it never handles a private key."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import connect_github  # noqa: E402

FAMILY = ["someone/LUCY-assistant", "someone/Alpha", "someone/Beta"]
CLIENT_ID = "Iv1.test0123456789ab"


def write_manifest(path: Path) -> Path:
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\nBeta https://github.com/someone/Beta.git\n",
        encoding="utf-8",
    )
    return path


def write_app(path: Path) -> Path:
    path.write_text(
        json.dumps({"slug": "lucy-assistant-family-ci", "client_id": CLIENT_ID}),
        encoding="utf-8",
    )
    return path


class FakeGitHub:
    def __init__(self, *, signed_in: bool = True):
        self.signed_in = signed_in
        self.commands: list[tuple[str, ...]] = []

    def run(self, args, **kwargs):
        argv = tuple(args)
        self.commands.append(argv)
        code, out = 0, ""
        if argv[:3] == ("gh", "auth", "status"):
            code = 0 if self.signed_in else 1
        elif argv[:3] == ("gh", "auth", "login"):
            self.signed_in = True
        elif argv[:2] == ("gh", "api") and argv[2] == "users/someone":
            out = json.dumps({"id": 555, "type": "User"})
        elif argv[:3] == ("gh", "workflow", "run"):
            pass
        else:
            raise AssertionError(f"unexpected gh call: {argv}")
        return subprocess.CompletedProcess(args, code, out, "")


def connect(tmp_path: Path, gh: FakeGitHub, **overrides) -> int:
    options: dict[str, object] = {
        "repos_file": write_manifest(tmp_path / "repos.txt"),
        "app_file": write_app(tmp_path / "family-app.json"),
        "dry_run": False,
        "interactive": False,
        "run": gh.run,
        "open_browser": lambda url: None,
        "confirm": lambda message: "",
    }
    options.update(overrides)
    return connect_github.connect(**options)


def test_dry_run_opens_nothing_and_creates_no_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    opened: list[str] = []
    assert connect(tmp_path, gh, dry_run=True, open_browser=lambda url: opened.append(url)) == 0
    assert opened == []
    assert not any(command[:3] == ("gh", "secret", "set") for command in gh.commands)
    out = capsys.readouterr().out
    assert "lucy-assistant-family-ci" in out
    assert "no token, private key, or Actions secret" in out
    assert "nothing was changed" in out


def test_install_opens_shared_app_and_triggers_ci(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    opened: list[str] = []
    prompted: list[str] = []
    assert (
        connect(
            tmp_path,
            gh,
            interactive=True,
            open_browser=lambda url: opened.append(url),
            confirm=lambda message: prompted.append(message) or "",
        )
        == 0
    )
    assert opened == [
        "https://github.com/apps/lucy-assistant-family-ci/installations/new/permissions?target_id=555"
    ]
    assert prompted
    assert not any(command[:3] == ("gh", "secret", "set") for command in gh.commands)
    assert (
        "gh",
        "workflow",
        "run",
        "CI",
        "--repo",
        "someone/Alpha",
        "--ref",
        "main",
    ) in gh.commands
    out = capsys.readouterr().out
    assert "Only select repositories" in out
    assert "CI started" in out


def test_non_interactive_install_does_not_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert connect(tmp_path, gh, interactive=False) == 0
    assert not any(command[:3] == ("gh", "workflow", "run") for command in gh.commands)
    assert "Finish the installation" in capsys.readouterr().out


def test_not_signed_in_without_a_terminal_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(signed_in=False)
    assert connect(tmp_path, gh) == 1
    assert "gh auth login" in capsys.readouterr().err


def test_signs_in_first_when_a_terminal_is_available(tmp_path: Path) -> None:
    gh = FakeGitHub(signed_in=False)
    assert connect(tmp_path, gh, interactive=True) == 0
    assert connect_github.LOGIN in gh.commands


def test_mixed_owners_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "mixed.txt"
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\nBeta https://github.com/other/Beta.git\n",
        encoding="utf-8",
    )
    gh = FakeGitHub()
    assert connect(tmp_path, gh, repos_file=path) == 1
    assert gh.commands == []


def test_main_wires_flags(tmp_path: Path) -> None:
    gh = FakeGitHub()
    manifest = write_manifest(tmp_path / "repos.txt")
    app = write_app(tmp_path / "family-app.json")
    assert (
        connect_github.main(
            ["--dry-run", "--repos-file", str(manifest), "--app-file", str(app)],
            run=gh.run,
        )
        == 0
    )
