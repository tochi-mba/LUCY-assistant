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
PROVE_NAME = "Prove this job may read the family"
MINT_NAME = "Mint a one-hour read-only token from the family's GitHub App"
AUTH_NAME = "Let git read the family's private repositories"
MINTED = "${{ steps.family-token.outputs.token }}"


def workflow() -> dict:
    # BaseLoader keeps GitHub's `on` key a string (YAML 1.1 calls it a boolean).
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def index_of(steps: list[dict], name: str) -> int:
    found = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert len(found) == 1, f"{name!r} appears {len(found)} times"
    return found[0]


def mint_block(steps: list[dict]) -> tuple[dict, dict]:
    prove, mint = index_of(steps, PROVE_NAME), index_of(steps, MINT_NAME)
    assert mint == prove + 1
    return steps[prove], steps[mint]


def auth_steps() -> list[dict]:
    return [
        step
        for job in workflow()["jobs"].values()
        for step in job["steps"]
        if step.get("name") == AUTH_NAME
    ]


def test_the_app_secrets_are_declared_and_optional() -> None:
    declared = workflow()["on"]["workflow_call"]["secrets"]
    assert set(declared) == {"FAMILY_APP_CLIENT_ID", "FAMILY_APP_PRIVATE_KEY"}
    assert all(secret["required"] == "false" for secret in declared.values())


def test_no_job_reads_a_long_lived_family_token() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets.FAMILY_GITHUB_TOKEN" not in text
    assert text.count("permission-contents: read") == text.count(MINT_NAME)


def test_the_minting_block_is_identical_wherever_it_appears() -> None:
    blocks = [
        mint_block(job["steps"])
        for job in workflow()["jobs"].values()
        if any(step.get("name") == MINT_NAME for step in job["steps"])
    ]
    assert len(blocks) == 10
    assert all(block == blocks[0] for block in blocks)
    prove, mint = blocks[0]
    assert prove["id"] == "family-app"
    assert prove["env"] == {"APP_ID": "${{ secrets.FAMILY_APP_CLIENT_ID }}"}
    assert mint["id"] == "family-token"
    assert mint["if"] == "${{ steps.family-app.outputs.installed == 'true' }}"
    assert mint["uses"].startswith("actions/create-github-app-token@v3")
    assert mint["with"] == {
        "client-id": "${{ secrets.FAMILY_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.FAMILY_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "permission-contents": "read",
    }


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
        mint = index_of(steps, MINT_NAME)
        auth = index_of(steps, AUTH_NAME)
        assert mint < auth < min(fetches), name
        if name == "tests":
            install_git = next(
                i for i, step in enumerate(steps) if "apt-get install" in step.get("run", "")
            )
            assert install_git < index_of(steps, PROVE_NAME)
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
    assert steps[0]["env"] == {"FAMILY_GITHUB_TOKEN": MINTED}


def test_docker_passes_the_minted_token_as_a_buildkit_secret() -> None:
    steps = workflow()["jobs"]["docker"]["steps"]
    build = index_of(steps, "Build")
    assert index_of(steps, MINT_NAME) < build
    assert "--secret id=github_token,env=FAMILY_GITHUB_TOKEN" in steps[build]["run"]
    assert "--build-arg" not in steps[build]["run"]
    assert steps[build]["env"]["FAMILY_GITHUB_TOKEN"] == MINTED


def test_meta_checkout_uses_the_minted_token_without_persisting_it() -> None:
    steps = workflow()["jobs"]["parity"]["steps"]
    checkout = next(i for i, step in enumerate(steps) if "repository" in step.get("with", {}))
    assert index_of(steps, MINT_NAME) < checkout
    assert (
        steps[checkout]["with"]["token"]
        == "${{ steps.family-token.outputs.token || github.token }}"
    )
    assert steps[checkout]["with"]["persist-credentials"] == "false"


def test_prove_step_reports_presence_without_printing_the_secret(tmp_path: Path) -> None:
    bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file():
        pytest.skip("bash is required for the isolated shell test")
    prove = mint_block(workflow()["jobs"]["lock"]["steps"])[0]
    for app_id, expected in (("", "installed=false"), ("12345", "installed=true")):
        output = tmp_path / "output"
        output.write_text("", encoding="utf-8")
        env = {**os.environ, "APP_ID": app_id, "GITHUB_OUTPUT": output.as_posix()}
        result = subprocess.run(
            [str(bash), "-c", prove["run"]], env=env, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert output.read_text(encoding="utf-8").strip() == expected
        assert "12345" not in result.stdout + result.stderr


@pytest.mark.parametrize("token", ["", "test-only-not-a-real-token"])
def test_authentication_shell_with_and_without_a_token(tmp_path: Path, token: str) -> None:
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


def test_meta_ci_installs_the_test_dependencies() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--with pyyaml" in ci
    assert "--with cryptography" in ci
