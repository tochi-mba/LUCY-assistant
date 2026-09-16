"""share_github.py copies a browser login into Actions without printing it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import share_github  # noqa: E402

SENTINEL = "gho_test-only-never-print-this-credential"


def write_manifest(path: Path) -> Path:
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\n"
        "Beta https://github.com/someone/Beta.git\n",
        encoding="utf-8",
    )
    return path


class FakeGitHub:
    def __init__(self, *, signed_in: bool = True, token: str = SENTINEL, fail: str | None = None):
        self.signed_in = signed_in
        self.token = token
        self.fail = fail
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(self, args, **kwargs):
        argv = tuple(args)
        supplied = kwargs.get("input")
        self.calls.append((argv, supplied))
        stdout = ""
        stderr = ""
        code = 0
        if argv[:3] == ("gh", "auth", "status"):
            code = 0 if self.signed_in else 1
        elif argv[:3] == ("gh", "auth", "login"):
            self.signed_in = argv[-1] == "--web" and self.fail != "login"
            code = 0 if self.signed_in else 1
        elif argv[:3] == ("gh", "auth", "token"):
            stdout = self.token + "\n"
            code = 0 if self.token else 1
        elif argv[:3] == ("gh", "secret", "set"):
            if self.fail == "secret":
                code = 1
                stderr = f"denied {self.token}\n" if "leak" in (self.fail or "") else "denied\n"
            elif supplied != self.token:
                code = 1
                stderr = "missing body\n"
        else:
            raise AssertionError(f"unexpected gh call: {argv}")
        return subprocess.CompletedProcess(args, code, stdout, stderr)


def test_targets_put_the_meta_repo_first(tmp_path: Path) -> None:
    write_manifest(tmp_path / "repos.txt")
    assert share_github.targets(tmp_path / "repos.txt") == [
        "someone/LUCY-assistant",
        "someone/Alpha",
        "someone/Beta",
    ]


def test_installs_the_secret_from_the_session_and_never_prints_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    code = share_github.share(
        repos_file=write_manifest(tmp_path / "repos.txt"),
        dry_run=False,
        interactive=False,
        run=gh,
    )
    assert code == 0
    repos = [call[0][5] for call in gh.calls if call[0][:3] == ("gh", "secret", "set")]
    assert repos == ["someone/LUCY-assistant", "someone/Alpha", "someone/Beta"]
    assert all(call[1] == SENTINEL for call in gh.calls if call[0][:3] == ("gh", "secret", "set"))
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert "3 repositories" in captured.out


def test_dry_run_does_not_read_or_set_the_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    code = share_github.share(
        repos_file=write_manifest(tmp_path / "repos.txt"),
        dry_run=True,
        interactive=False,
        run=gh,
    )
    assert code == 0
    assert [call[0][:3] for call in gh.calls] == [("gh", "auth", "status")]
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "someone/Alpha" in output
    assert SENTINEL not in output


def test_opens_the_browser_when_not_signed_in(tmp_path: Path) -> None:
    gh = FakeGitHub(signed_in=False)
    assert (
        share_github.share(
            repos_file=write_manifest(tmp_path / "repos.txt"),
            dry_run=False,
            interactive=True,
            run=gh,
        )
        == 0
    )
    login = next(call[0] for call in gh.calls if call[0][:3] == ("gh", "auth", "login"))
    assert login == share_github.LOGIN
    assert "--web" in login


def test_noninteractive_without_a_session_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(signed_in=False)
    code = share_github.share(
        repos_file=write_manifest(tmp_path / "repos.txt"),
        dry_run=False,
        interactive=False,
        run=gh,
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "--web" in err
    assert SENTINEL not in err
    assert not any(call[0][:3] == ("gh", "secret", "set") for call in gh.calls)


def test_secret_failure_does_not_echo_the_credential(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(fail="secret")
    code = share_github.share(
        repos_file=write_manifest(tmp_path / "repos.txt"),
        dry_run=False,
        interactive=False,
        run=gh,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    assert "someone/LUCY-assistant" in captured.err


def test_mixed_owners_are_refused_before_calling_github(tmp_path: Path) -> None:
    path = tmp_path / "repos.txt"
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\n"
        "Beta https://github.com/other/Beta.git\n",
        encoding="utf-8",
    )
    gh = FakeGitHub()
    assert share_github.share(repos_file=path, dry_run=False, interactive=False, run=gh) == 2
    assert gh.calls == []


def test_missing_manifest_is_usage_error(tmp_path: Path) -> None:
    gh = FakeGitHub()
    assert (
        share_github.share(
            repos_file=tmp_path / "repos.txt",
            dry_run=True,
            interactive=False,
            run=gh,
        )
        == 2
    )
    assert gh.calls == []
