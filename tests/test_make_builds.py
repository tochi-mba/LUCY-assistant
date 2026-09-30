"""Execute Make build recipes with fake gh/docker commands; no real credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICES = [
    row.split()[0]
    for row in (ROOT / "repos.txt").read_text().splitlines()
    if row.strip() and not row.lstrip().startswith("#")
]

HARNESS = r"""
set -eu
gh() {
  test "$*" = "auth token"
  printf '%s' 'make-test-credential'
}
docker() {
  case "$*" in
    *build*) test "${GITHUB_TOKEN:-}" = 'make-test-credential' ;;
    *) test -z "${GITHUB_TOKEN:-}" ;;
  esac
  printf 'docker %s\n' "$*"
}
export -f gh docker
make --no-print-directory SHELL=bash "$1"
"""


@pytest.mark.parametrize(
    "repo,target",
    [(".", "images"), (".", "up"), (".", "down")] + [(repo, "docker") for repo in SERVICES],
)
def test_build_recipe_passes_token_only_to_builds(tmp_path: Path, repo: str, target: str) -> None:
    source = ROOT / repo / "Makefile"
    if not source.exists():
        pytest.skip(f"{repo} checkout is not present")
    bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file() or not shutil.which("make"):
        pytest.skip("bash and make are required for the isolated recipe test")
    if os.name == "nt" and target in {"up", "down"}:
        # GNU make on Windows runs a recipe line with no shell characters directly, so a
        # bash function cannot stand in for docker there. The build recipes contain $( ).
        pytest.skip("make on Windows bypasses the shell for plain recipe lines")
    (tmp_path / "Makefile").write_bytes(source.read_bytes())
    env = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "FAMILY_GITHUB_TOKEN"):
        env.pop(name, None)
    result = subprocess.run(
        [str(bash), "-c", HARNESS, "--", target],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "make-test-credential" not in result.stdout + result.stderr
    calls = [line for line in result.stdout.splitlines() if line.startswith("docker ")]
    if repo != ".":
        assert len(calls) == 1
        assert calls[0].startswith("docker build --secret id=github_token,env=GITHUB_TOKEN -t ")
        assert calls[0].endswith(":local .")
    elif target == "images":
        assert calls == ["docker compose build"]
    elif target == "up":
        # Make itself echoes the up recipe; the fake docker call echoes it again.
        assert calls == [
            "docker compose build",
            "docker compose up -d --no-build",
            "docker compose up -d --no-build",
        ]
    else:
        assert calls == ["docker compose down", "docker compose down"]
