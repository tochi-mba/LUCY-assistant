"""The family compose wrapper keeps credentials scoped and overlays automatic."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compose  # type: ignore[import-not-found]  # noqa: E402


class Runner:
    def __init__(self, *, token: str = "build-token", token_status: int = 0) -> None:
        self.token = token
        self.token_status = token_status
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if args[:3] == ["gh", "auth", "token"]:
            return subprocess.CompletedProcess(args, self.token_status, self.token, "")
        return subprocess.CompletedProcess(args, 0, "", "")


def test_command_includes_the_local_overlay_only_when_present(tmp_path: Path) -> None:
    assert compose.command("up", tmp_path) == ["docker", "compose", "up", "-d", "--no-build"]
    (tmp_path / "docker-compose.local.yml").write_text("services: {}\n", encoding="utf-8")
    assert compose.command("up", tmp_path) == [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.local.yml",
        "up",
        "-d",
        "--no-build",
    ]


def test_build_fetches_a_token_without_putting_it_in_the_command(tmp_path: Path) -> None:
    runner = Runner()
    assert compose.execute("build", root=tmp_path, source={}, run=runner) == 0
    assert runner.calls[0][0] == ["gh", "auth", "token"]
    args, kwargs = runner.calls[1]
    assert args == ["docker", "compose", "build"]
    assert runner.token not in " ".join(args)
    assert kwargs["env"]["GITHUB_TOKEN"] == runner.token


@pytest.mark.parametrize("action", ["up", "down"])
def test_non_build_actions_remove_an_ambient_build_token(tmp_path: Path, action: str) -> None:
    runner = Runner()
    assert (
        compose.execute(action, root=tmp_path, source={"GITHUB_TOKEN": "ambient"}, run=runner) == 0
    )
    assert len(runner.calls) == 1
    assert "GITHUB_TOKEN" not in runner.calls[0][1]["env"]


@pytest.mark.parametrize("token,status", [("", 0), ("ignored", 1)])
def test_build_refuses_a_missing_github_login(token: str, status: int) -> None:
    with pytest.raises(compose.ComposeError, match="gh auth login"):
        compose.environment("build", {}, run=Runner(token=token, token_status=status))


def test_main_renders_a_safe_login_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        compose, "execute", lambda action: (_ for _ in ()).throw(compose.ComposeError("sign in"))
    )
    assert compose.main(["build"]) == 2
    assert capsys.readouterr().err == "error: sign in\n"
