"""share_github.py installs a read-only fine-grained token as a secret without printing it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import share_github  # noqa: E402

FINE = "github_pat_test-only-never-print-this-credential"
CLASSIC = "gho_test-only-never-print-this-session"


def write_manifest(path: Path) -> Path:
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\nBeta https://github.com/someone/Beta.git\n",
        encoding="utf-8",
    )
    return path


class FakeGitHub:
    """A stand-in for ``gh``; the real one is never called from these tests."""

    def __init__(
        self,
        *,
        signed_in: bool = True,
        login_ok: bool = True,
        readable: frozenset[str] | None = None,
        secret_error: str | None = None,
    ):
        self.signed_in = signed_in
        self.login_ok = login_ok
        self.readable = readable
        self.secret_error = secret_error
        self.calls: list[tuple[tuple[str, ...], str | None, dict[str, str] | None]] = []

    def __call__(self, args, **kwargs):
        argv = tuple(args)
        supplied = kwargs.get("input")
        env = kwargs.get("env")
        self.calls.append((argv, supplied, env))
        stdout = stderr = ""
        code = 0
        if argv[:3] == ("gh", "auth", "status"):
            code = 0 if self.signed_in else 1
        elif argv[:3] == ("gh", "auth", "login"):
            self.signed_in = self.login_ok
            code = 0 if self.login_ok else 1
        elif argv[:2] == ("gh", "api"):
            # Reading a repository must use the candidate token, not the user's session.
            assert env is not None and env.get("GH_TOKEN") == FINE
            repo = argv[2].removeprefix("repos/")
            code = 0 if self.readable is None or repo in self.readable else 1
        elif argv[:3] == ("gh", "secret", "set"):
            if self.secret_error is not None:
                code, stderr = 1, self.secret_error
            elif supplied != FINE:
                code, stderr = 1, "missing body\n"
        else:
            raise AssertionError(f"unexpected gh call: {argv}")
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def secrets_set(self) -> list[str]:
        return [call[0][5] for call in self.calls if call[0][:3] == ("gh", "secret", "set")]


def run(tmp_path: Path, gh: FakeGitHub, **overrides) -> int:
    options = dict(
        repos_file=write_manifest(tmp_path / "repos.txt"),
        dry_run=False,
        interactive=False,
        from_stdin=False,
        allow_any=False,
        run=gh,
        environ={"FAMILY_GITHUB_TOKEN": FINE},
    )
    options.update(overrides)
    return share_github.share(**options)


def test_targets_put_the_meta_repo_first(tmp_path: Path) -> None:
    write_manifest(tmp_path / "repos.txt")
    assert share_github.targets(tmp_path / "repos.txt") == [
        "someone/LUCY-assistant",
        "someone/Alpha",
        "someone/Beta",
    ]


def test_installs_the_token_on_every_repository_and_never_prints_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert run(tmp_path, gh) == 0
    assert gh.secrets_set() == ["someone/LUCY-assistant", "someone/Alpha", "someone/Beta"]
    assert all(call[1] == FINE for call in gh.calls if call[0][:3] == ("gh", "secret", "set"))
    assert not any(FINE in part for call in gh.calls for part in call[0])
    captured = capsys.readouterr()
    assert FINE not in captured.out + captured.err
    assert "3 repositories" in captured.out


def test_dry_run_checks_access_but_sets_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert run(tmp_path, gh, dry_run=True) == 0
    assert gh.secrets_set() == []
    assert [call[0][2] for call in gh.calls if call[0][:2] == ("gh", "api")] == [
        "repos/someone/LUCY-assistant",
        "repos/someone/Alpha",
        "repos/someone/Beta",
    ]
    output = capsys.readouterr().out
    assert "nothing was changed" in output
    assert "someone/Alpha" in output
    assert FINE not in output


def test_a_token_that_cannot_read_a_repository_is_named_before_anything_is_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(readable=frozenset({"someone/LUCY-assistant", "someone/Alpha"}))
    assert run(tmp_path, gh) == 1
    assert gh.secrets_set() == []
    err = capsys.readouterr().err
    assert "cannot read someone/Beta" in err
    assert "repository list" in err
    assert FINE not in err


def test_classic_or_session_tokens_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert run(tmp_path, gh, environ={"FAMILY_GITHUB_TOKEN": CLASSIC}) == 2
    assert not any(call[0][:2] == ("gh", "api") for call in gh.calls)
    err = capsys.readouterr().err
    assert "fine-grained" in err
    assert share_github.NEW_TOKEN_URL in err
    assert CLASSIC not in err


def test_allow_any_token_overrides_the_shape_check(tmp_path: Path) -> None:
    class Any(FakeGitHub):
        def __call__(self, args, **kwargs):
            if tuple(args)[:2] == ("gh", "api"):
                assert kwargs["env"]["GH_TOKEN"] == CLASSIC
                self.calls.append((tuple(args), None, kwargs["env"]))
                return subprocess.CompletedProcess(args, 0, "", "")
            return super().__call__(args, **kwargs)

    gh = Any()
    code = run(tmp_path, gh, environ={"FAMILY_GITHUB_TOKEN": CLASSIC}, allow_any=True, dry_run=True)
    assert code == 0


def test_stdin_token_is_read_from_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(FINE + "\n"))
    gh = FakeGitHub()
    assert run(tmp_path, gh, from_stdin=True, environ={}) == 0
    assert gh.secrets_set() == ["someone/LUCY-assistant", "someone/Alpha", "someone/Beta"]


def test_prompt_is_hidden_and_explains_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return FINE

    monkeypatch.setattr(share_github.getpass, "getpass", fake_getpass)
    gh = FakeGitHub()
    assert run(tmp_path, gh, interactive=True, environ={}) == 0
    assert prompts == ["Paste the token (not shown): "]
    out = capsys.readouterr().out
    assert share_github.NEW_TOKEN_URL in out
    assert "read-only" in out
    assert FINE not in out


def test_no_token_without_a_terminal_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert run(tmp_path, gh, environ={}) == 2
    err = capsys.readouterr().err
    assert "FAMILY_GITHUB_TOKEN" in err
    assert "--stdin" in err
    assert gh.secrets_set() == []


def test_empty_token_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, FakeGitHub(), environ={"FAMILY_GITHUB_TOKEN": "  "}) == 2
    assert "no token was read" in capsys.readouterr().err


def test_signs_in_with_gh_when_no_session_and_a_terminal(tmp_path: Path) -> None:
    gh = FakeGitHub(signed_in=False)
    assert run(tmp_path, gh, interactive=True) == 0
    login = next(call[0] for call in gh.calls if call[0][:3] == ("gh", "auth", "login"))
    assert login == share_github.LOGIN
    assert "--web" not in login  # gh itself offers the browser or a pasted token


def test_cancelled_sign_in_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh = FakeGitHub(signed_in=False, login_ok=False)
    assert run(tmp_path, gh, interactive=True) == 2
    assert "did not complete" in capsys.readouterr().err


def test_noninteractive_without_a_session_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(signed_in=False)
    assert run(tmp_path, gh) == 2
    err = capsys.readouterr().err
    assert "gh auth login" in err
    assert not any(call[0][:3] == ("gh", "secret", "set") for call in gh.calls)


def test_secret_failure_does_not_echo_the_credential(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(secret_error=f"denied {FINE}\n")
    assert run(tmp_path, gh) == 1
    captured = capsys.readouterr()
    assert FINE not in captured.out + captured.err
    assert "someone/LUCY-assistant: secret set failed" in captured.err


def test_mixed_owners_are_refused_before_calling_github(tmp_path: Path) -> None:
    path = tmp_path / "mixed.txt"
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\nBeta https://github.com/other/Beta.git\n",
        encoding="utf-8",
    )
    gh = FakeGitHub()
    assert run(tmp_path, gh, repos_file=path) == 2
    assert gh.calls == []


@pytest.mark.parametrize(
    "line",
    ["Alpha not-a-url\n", "Alpha https://github.com/someone/Other.git\n", "Alpha\n"],
)
def test_malformed_manifest_lines_are_refused(tmp_path: Path, line: str) -> None:
    path = tmp_path / "repos.txt"
    path.write_text(line, encoding="utf-8")
    with pytest.raises(share_github.ShareError):
        share_github.read_family(path)


def test_missing_manifest_is_usage_error(tmp_path: Path) -> None:
    gh = FakeGitHub()
    assert run(tmp_path, gh, repos_file=tmp_path / "missing.txt") == 2
    assert gh.calls == []


def test_main_wires_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGitHub()
    monkeypatch.setenv("FAMILY_GITHUB_TOKEN", FINE)
    manifest = write_manifest(tmp_path / "repos.txt")
    assert share_github.main(["--dry-run", "--repos-file", str(manifest)], run=gh) == 0
    assert gh.secrets_set() == []
