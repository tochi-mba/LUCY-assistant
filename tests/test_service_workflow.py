"""Private dependency authentication in the reusable workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/service.yml"
AUTH_NAME = "Let git read the family's private repositories"


def workflow() -> dict:
    # BaseLoader keeps GitHub's `on` key a string (YAML 1.1 calls it a boolean).
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def auth_steps() -> list[dict]:
    return [
        step
        for job in workflow()["jobs"].values()
        for step in job["steps"]
        if step.get("name") == AUTH_NAME
    ]


def test_required_family_secret_is_declared() -> None:
    secret = workflow()["on"]["workflow_call"]["secrets"]["FAMILY_GITHUB_TOKEN"]
    assert secret["required"] == "true"


def test_every_dependency_fetch_is_authenticated_first() -> None:
    checked = []
    for name, job in workflow()["jobs"].items():
        steps = job["steps"]
        fetches = [
            index
            for index, step in enumerate(steps)
            if any(command in step.get("run", "") for command in ("uv sync", "uv lock"))
        ]
        if not fetches:
            continue
        authentication = [i for i, step in enumerate(steps) if step.get("name") == AUTH_NAME]
        assert len(authentication) == 1, name
        assert authentication[0] < min(fetches), name
        if name == "tests":
            install_git = next(
                i for i, step in enumerate(steps) if "apt-get install" in step.get("run", "")
            )
            assert install_git < authentication[0]
        checked.append(name)
    assert set(checked) == {
        "lock",
        "format",
        "lint",
        "types",
        "imports",
        "tests",
        "generated",
        "live-browser",
    }
    steps = auth_steps()
    assert all(step == steps[0] for step in steps)
    assert steps[0]["env"] == {"FAMILY_GITHUB_TOKEN": "${{ secrets.FAMILY_GITHUB_TOKEN }}"}


def test_docker_passes_a_buildkit_secret_from_the_environment() -> None:
    steps = workflow()["jobs"]["docker"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build")
    assert "--secret id=github_token,env=FAMILY_GITHUB_TOKEN" in build["run"]
    assert "--build-arg" not in build["run"]
    assert build["env"]["FAMILY_GITHUB_TOKEN"] == "${{ secrets.FAMILY_GITHUB_TOKEN }}"


def test_meta_checkout_uses_family_token_without_persisting_it() -> None:
    steps = workflow()["jobs"]["parity"]["steps"]
    checkout = next(step["with"] for step in steps if "repository" in step.get("with", {}))
    assert checkout["token"] == "${{ secrets.FAMILY_GITHUB_TOKEN || github.token }}"
    assert checkout["persist-credentials"] == "false"


@pytest.mark.parametrize("token", ["", "test-only-not-a-real-token"])
def test_authentication_shell_with_and_without_a_secret(tmp_path: Path, token: str) -> None:
    bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file() or not shutil.which("git"):
        pytest.skip("bash and git are required for the isolated auth shell test")
    config = tmp_path / "gitconfig"
    env = os.environ.copy()
    env.update(
        FAMILY_GITHUB_TOKEN=token, GIT_CONFIG_GLOBAL=config.as_posix(), GIT_CONFIG_NOSYSTEM="1"
    )
    step = auth_steps()[0]
    result = subprocess.run(
        [str(bash), "-c", step["run"]], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "test-only-not-a-real-token" not in result.stdout + result.stderr
    if token:
        configured = subprocess.run(
            [
                "git",
                "config",
                "--global",
                "--get",
                f"url.https://x-access-token:{token}@github.com/.insteadOf",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert configured.stdout.strip() == "https://github.com/"
    else:
        assert not config.exists()


def test_meta_ci_installs_yaml_for_contract_tests() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--with pyyaml" in ci
