"""Private dependency authentication in the reusable workflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/service.yml"
TOKEN_ACTION = ROOT / ".github/actions/family-token/action.yml"
MINT_NAME = "Mint a one-hour token for this app installation"
AUTH_NAME = "Let git read the family's private repositories"
MINTED = "${{ steps.family-token.outputs.token }}"
BROKER = "https://lucy-family-ci-token-broker.lucy-assistant-family.workers.dev/v1/token"


def workflow() -> dict:
    # BaseLoader keeps GitHub's `on` key a string (YAML 1.1 calls it a boolean).
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def index_of(steps: list[dict], name: str) -> int:
    found = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert len(found) == 1, f"{name!r} appears {len(found)} times"
    return found[0]


def auth_steps() -> list[dict]:
    return [
        step
        for job in workflow()["jobs"].values()
        for step in job["steps"]
        if step.get("name") == AUTH_NAME
    ]


def test_the_workflow_uses_oidc_and_declares_no_user_secrets() -> None:
    called = workflow()["on"]["workflow_call"]
    assert "secrets" not in called
    assert workflow()["permissions"] == {"contents": "read", "id-token": "write"}


def test_no_job_reads_an_app_key_or_long_lived_token() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "FAMILY_APP_PRIVATE_KEY" not in text


def test_token_action_uses_github_oidc_and_masks_the_broker_result() -> None:
    source = TOKEN_ACTION.read_text(encoding="utf-8")
    action = yaml.safe_load(source)
    assert action["runs"]["using"] == "composite"
    shell = action["runs"]["steps"][0]["run"]
    assert "ACTIONS_ID_TOKEN_REQUEST_TOKEN" in shell
    assert "audience=${OIDC_AUDIENCE}" in shell
    assert 'echo "::add-mask::$token"' in shell
    assert 'echo "token=$token" >> "$GITHUB_OUTPUT"' in shell
    assert "FAMILY_APP_PRIVATE_KEY" not in source
    assert "secrets." not in source


def test_the_minting_block_is_identical_wherever_it_appears() -> None:
    blocks = [
        job["steps"][index_of(job["steps"], MINT_NAME)]
        for job in workflow()["jobs"].values()
        if any(step.get("name") == MINT_NAME for step in job["steps"])
    ]
    assert len(blocks) == 10
    assert all(block == blocks[0] for block in blocks)
    mint = blocks[0]
    assert mint["id"] == "family-token"
    assert mint["uses"] == "tochi-mba/LUCY-assistant/.github/actions/family-token@v1"
    assert mint["with"] == {"broker-url": BROKER}


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
            assert install_git < mint
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


@pytest.mark.parametrize("token", ["", "test-only-not-a-real-token"])
def test_authentication_shell_with_and_without_a_token(tmp_path: Path, token: str) -> None:
    bash = Path("C:/Program Files/Git/bin/bash.exe") if os.name == "nt" else shutil.which("bash")
    if not bash or not Path(bash).is_file() or not shutil.which("git"):
        pytest.skip("bash and git are required for the isolated auth shell test")
    config = tmp_path / "gitconfig"
    env = os.environ.copy()
    env.update(
        FAMILY_GITHUB_TOKEN=token,
        GIT_CONFIG_GLOBAL=config.as_posix(),
        GIT_CONFIG_NOSYSTEM="1",
    )
    step = auth_steps()[0]
    result = subprocess.run(
        [str(bash), "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
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


def test_meta_ci_runs_the_gates_against_its_own_workflow() -> None:
    # This repository owns service.yml, so it calls it by local path. Pinning itself to a
    # tag would mean a change to the workflow is never tested by the repository that made
    # it. The suite's dependencies are the project's dev group now, not `--with` flags.
    ci = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert ci["jobs"]["service"]["uses"] == "./.github/workflows/service.yml"
    assert ci["permissions"]["id-token"] == "write"
    assert "npm run check" in (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_the_suite_dependencies_are_declared_rather_than_improvised() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = " ".join(project["dependency-groups"]["dev"])
    for name in ("pytest", "pyyaml", "cryptography", "pytest-cov", "respx"):
        assert name in dev, name


def test_the_reusable_workflow_does_not_share_a_concurrency_group_with_its_caller() -> None:
    # `github.workflow` is the CALLER's name inside a reusable workflow. When both sides
    # compute the same group and cancel-in-progress is on, the called jobs land in the group
    # the calling run already occupies and never start -- a whole workflow silently missing,
    # with no job and no annotation to explain it.
    called = workflow()["concurrency"]["group"]
    caller = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )["concurrency"]["group"]
    assert called != caller
