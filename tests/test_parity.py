"""Parity checker: every family check has a passing case and a failing case."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import parity  # noqa: E402

GOLDEN_NAME = "Demo-api"

GOLDEN_CI = """\
name: CI
permissions:
  contents: read
  id-token: write
jobs:
  service:
    uses: owner/LUCY-assistant/.github/workflows/service.yml@v1
"""

GOLDEN_DOCKERFILE = """\
# syntax=docker/dockerfile:1
FROM python:3.11-slim
RUN --mount=type=secret,id=github_token,required=false \\
    uv sync --no-install-project --no-dev
RUN --mount=type=cache,target=/root/.cache/uv \\
    --mount=id=github_token,type=secret \\
    uv sync --no-cache
"""

GOLDEN_MAKEFILE = """\
help:
install:
fmt:
lint:
type:
imports:
test:
cov:
check: lint type imports test
run:
docker:
clean:
"""

GOLDEN_PYPROJECT = """\
[project]
name = "demo"
dependencies = ["keyring-client"]

[dependency-groups]
dev = ["pytest"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
strict = true

[tool.coverage.report]
fail_under = 100

[tool.coverage.run]
branch = true

[tool.pytest.ini_options]
filterwarnings = ["error"]

[[tool.importlinter.contracts]]
name = "layers"
type = "layers"
layers = ["demo"]
"""

GOLDEN_CONFIG = """\
from keyring_client import ExactAudience

env_prefix = "DEMO_"
extra = "forbid"


def check_for_unknown_env_vars() -> None:
    return None


ROUTES = ("/healthy", "/ready")
"""

DOCS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs/architecture.md",
    "docs/api.md",
    "docs/operations.md",
    "docs/testing.md",
    "docs/adr/README.md",
)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_lines(path: Path, count: int) -> None:
    write_text(path, "".join(f"x = {index}\n" for index in range(count)))


def write_golden(root: Path) -> Path:
    write_text(root / ".github/workflows/ci.yml", GOLDEN_CI)
    write_text(root / "Dockerfile", GOLDEN_DOCKERFILE)
    write_text(root / "Makefile", GOLDEN_MAKEFILE)
    write_text(root / "pyproject.toml", GOLDEN_PYPROJECT)
    write_text(root / ".python-version", "3.12\n")
    write_text(root / "CLAUDE.md", "Read AGENTS.md first.\n")
    write_text(root / "CHANGELOG.md", "See https://keepachangelog.com/en/1.1.0/\n")
    write_text(root / ".pre-commit-config.yaml", "repos: []\n")
    write_text(root / ".editorconfig", "root = true\n")
    for relative in DOCS:
        write_text(root / relative, "# placeholder\n")
    write_text(root / "src/demo/__init__.py", "")
    write_text(root / "src/demo/core/__init__.py", "")
    write_text(root / "src/demo/core/config.py", GOLDEN_CONFIG)
    return root


def outcome_for(root: Path, check_id: str, name: str = GOLDEN_NAME) -> parity.Result:
    parent = root.parent
    report = parity.evaluate(parent, [name])[0]
    assert report.present
    for item in report.outcomes:
        if item.check.id == check_id:
            return item.result
    raise AssertionError(f"no outcome for {check_id}")


def fail(root: Path, check_id: str) -> None:
    """Mutate a golden checkout so ``check_id`` fails. One function per check."""
    if check_id == "make-targets":
        write_text(root / "Makefile", GOLDEN_MAKEFILE.replace("docker:\n", ""))
    elif check_id == "make-check":
        write_text(
            root / "Makefile",
            GOLDEN_MAKEFILE.replace("check: lint type imports test", "check: lint"),
        )
    elif check_id == "python-version":
        write_text(root / ".python-version", "3.10\n")
    elif check_id == "claude-md":
        write_text(root / "CLAUDE.md", "No pointer.\n")
    elif check_id == "docs":
        (root / "README.md").unlink()
    elif check_id == "changelog":
        write_text(root / "CHANGELOG.md", "# History\n")
    elif check_id == "pre-commit":
        (root / ".pre-commit-config.yaml").unlink()
    elif check_id == "editorconfig":
        (root / ".editorconfig").unlink()
    elif check_id == "ci-identity":
        write_text(
            root / ".github/workflows/ci.yml",
            GOLDEN_CI.replace("  id-token: write\n", ""),
        )
    elif check_id == "docker-secret":
        write_text(
            root / "Dockerfile",
            GOLDEN_DOCKERFILE.replace("    --mount=id=github_token,type=secret \\\n", ""),
        )
    elif check_id == "dev-group":
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.replace(
                '[dependency-groups]\ndev = ["pytest"]\n',
                '[project.optional-dependencies]\ndev = ["pytest"]\n',
            ),
        )
    elif check_id == "ruff":
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.replace("line-length = 100", "line-length = 88"),
        )
    elif check_id == "mypy-strict":
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.replace("strict = true", "strict = false"),
        )
    elif check_id == "coverage":
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.replace("fail_under = 100", "fail_under = 80"),
        )
    elif check_id == "pytest-warnings":
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.replace('filterwarnings = ["error"]', 'filterwarnings = ["ignore"]'),
        )
    elif check_id == "import-linter":
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.split("[[tool.importlinter.contracts]]", maxsplit=1)[0],
        )
    elif check_id == "config":
        write_text(
            root / "src/demo/core/config.py",
            GOLDEN_CONFIG.replace('env_prefix = "DEMO_"\n', ""),
        )
    elif check_id == "unknown-env":
        write_text(
            root / "src/demo/core/config.py",
            GOLDEN_CONFIG.replace(
                "def check_for_unknown_env_vars() -> None:\n    return None\n",
                "def something_else() -> None:\n    return None\n",
            ),
        )
    elif check_id == "no-pragma":
        write_text(
            root / "src/demo/core/config.py",
            GOLDEN_CONFIG + "\nvalue = 1  # pragma: no cover\n",
        )
    elif check_id == "max-file-lines":
        write_lines(root / "tests/huge.py", parity.MAX_FILE_LINES + 1)
    elif check_id == "health-routes":
        write_text(
            root / "src/demo/core/config.py",
            GOLDEN_CONFIG.replace('"/ready"', '"/live"'),
        )
    elif check_id == "keyring-client":
        write_text(
            root / "src/demo/core/config.py",
            GOLDEN_CONFIG.replace("from keyring_client import ExactAudience\n", ""),
        )
        write_text(
            root / "pyproject.toml",
            GOLDEN_PYPROJECT.replace('dependencies = ["keyring-client"]', "dependencies = []"),
        )
    else:
        raise AssertionError(f"no failing mutation for {check_id}")


def test_every_declared_check_has_a_failing_case() -> None:
    """parity.py refuses a check that has no failing case — this is that refusal."""
    import inspect

    source = inspect.getsource(fail)
    missing = [check.id for check in parity.CHECKS if f'"{check.id}"' not in source]
    assert missing == [], f"add a failing mutation for: {', '.join(missing)}"


@pytest.mark.parametrize("check_id", [check.id for check in parity.CHECKS])
def test_golden_passes(tmp_path: Path, check_id: str) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    result = outcome_for(root, check_id)
    assert result.status == parity.PASS, result.detail


@pytest.mark.parametrize("check_id", [check.id for check in parity.CHECKS])
def test_mutated_golden_fails(tmp_path: Path, check_id: str) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    fail(root, check_id)
    result = outcome_for(root, check_id)
    assert result.status == parity.FAIL, f"{check_id} did not fail: {result.status} {result.detail}"


def test_max_file_lines_allows_exactly_the_limit(tmp_path: Path) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    write_lines(root / "scripts/edge.py", parity.MAX_FILE_LINES)
    result = outcome_for(root, "max-file-lines")
    assert result.status == parity.PASS


def test_max_file_lines_scans_scripts_and_clients(tmp_path: Path) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    write_lines(root / "clients/python/huge.py", parity.MAX_FILE_LINES + 1)
    result = outcome_for(root, "max-file-lines")
    assert result.status == parity.FAIL
    assert "clients/python/huge.py" in result.detail


def test_keyring_client_is_na_on_the_issuer(tmp_path: Path) -> None:
    root = write_golden(tmp_path / "Keyring-api")
    write_text(
        root / "pyproject.toml",
        GOLDEN_PYPROJECT.replace('dependencies = ["keyring-client"]', "dependencies = []"),
    )
    write_text(
        root / "src/demo/core/config.py",
        GOLDEN_CONFIG.replace("from keyring_client import ExactAudience\n", ""),
    )
    result = outcome_for(root, "keyring-client", name="Keyring-api")
    assert result.status == parity.NOT_APPLICABLE


def test_missing_checkout_is_reported(tmp_path: Path) -> None:
    reports = parity.evaluate(tmp_path, ["No-such-api"])
    assert reports[0].present is False
    assert reports[0].ok is False
    text = parity.render_text(reports)
    assert "not checked out" in text


def test_render_json_round_trips(tmp_path: Path) -> None:
    write_golden(tmp_path / GOLDEN_NAME)
    reports = parity.evaluate(tmp_path, [GOLDEN_NAME])
    document = json.loads(parity.render_json(reports, tmp_path))
    assert document["ok"] is True
    ids = [row["id"] for row in document["repos"][0]["checks"]]
    assert ids == [check.id for check in parity.CHECKS]
    assert "max-file-lines" in ids


def test_cli_unknown_repo_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = parity.main(
        [
            "--root",
            str(tmp_path),
            "--repos-file",
            str(ROOT / "repos.txt"),
            "--repo",
            "NotARepo",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown repository" in err


def test_cli_json_exit_zero_on_golden(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_golden(tmp_path / GOLDEN_NAME)
    manifest = tmp_path / "repos.txt"
    manifest.write_text(
        f"{GOLDEN_NAME} https://example.invalid/{GOLDEN_NAME}.git\n", encoding="utf-8"
    )
    code = parity.main(["--root", str(tmp_path), "--repos-file", str(manifest), "--json"])
    assert code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True


def test_cli_exit_one_on_drift(tmp_path: Path) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    fail(root, "max-file-lines")
    manifest = tmp_path / "repos.txt"
    manifest.write_text(
        f"{GOLDEN_NAME} https://example.invalid/{GOLDEN_NAME}.git\n", encoding="utf-8"
    )
    code = parity.main(["--root", str(tmp_path), "--repos-file", str(manifest)])
    assert code == 1


def test_read_repo_names_skips_comments(tmp_path: Path) -> None:
    manifest = tmp_path / "repos.txt"
    manifest.write_text("# header\n\nAlpha https://x\n  Bravo https://y\n", encoding="utf-8")
    assert parity.read_repo_names(manifest) == ["Alpha", "Bravo"]


@pytest.mark.parametrize(
    "ci",
    [
        None,
        GOLDEN_CI.replace("  id-token: write\n", ""),
        GOLDEN_CI.replace("  id-token: write", "  id-token: read"),
        GOLDEN_CI.replace("  id-token: write", "  id-token: 'write'\n").replace(
            "    uses:", "    secrets: inherit\n    uses:"
        ),
        GOLDEN_CI.replace("permissions:\n", "other:\n  id-token: write\npermissions:\n").replace(
            "  id-token: write\njobs:", "jobs:"
        ),
        "jobs:\n  local:\n    steps:\n      - run: echo 'id-token: write'\n",
    ],
)
def test_ci_identity_rejects_missing_unrelated_or_secret_based_auth(
    tmp_path: Path, ci: str | None
) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    if ci is None:
        (root / ".github/workflows/ci.yml").unlink()
    else:
        write_text(root / ".github/workflows/ci.yml", ci)
    assert outcome_for(root, "ci-identity").status == parity.FAIL


def test_ci_identity_accepts_quoted_oidc_permission_and_other_jobs(
    tmp_path: Path,
) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    write_text(
        root / ".github/workflows/ci.yml",
        GOLDEN_CI.replace(
            "uses: owner/LUCY-assistant/.github/workflows/service.yml@v1",
            "uses: 'owner/LUCY-assistant/.github/workflows/service.yml@v1' # family",
        ).replace("id-token: write", "id-token: 'write' # broker")
        + ("  standalone:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n"),
    )
    assert outcome_for(root, "ci-identity").status == parity.PASS


@pytest.mark.parametrize(
    "dockerfile",
    [
        None,
        GOLDEN_DOCKERFILE.replace("# syntax=docker/dockerfile:1\n", ""),
        "\n" + GOLDEN_DOCKERFILE,
        GOLDEN_DOCKERFILE.replace("id=github_token", "id=another_token"),
        GOLDEN_DOCKERFILE.replace("type=secret", "type=cache"),
        GOLDEN_DOCKERFILE + "RUN uv sync --no-cache\n",
        "# syntax=docker/dockerfile:1\n# --mount=type=secret,id=github_token\nRUN uv sync\n",
        '# syntax=docker/dockerfile:1\nRUN echo "--mount=type=secret,id=github_token" && uv sync\n',
        "# syntax=docker/dockerfile:1\nRUN uv sync # --mount=type=secret,id=github_token\n",
        (
            "# syntax=docker/dockerfile:1\n"
            "RUN --mount=type=secret,id=github_token uv --version\nRUN uv sync\n"
        ),
        "# syntax=docker/dockerfile:1\nRUN uv sync \\",
        '# syntax=docker/dockerfile:1\nRUN uv sync "unterminated\n',
        "# syntax=docker/dockerfile:1\nRUN [invalid json]\n",
        "# syntax=docker/dockerfile:1\nFROM python:3.11\n",
    ],
)
def test_docker_secret_rejects_unprotected_syncs(tmp_path: Path, dockerfile: str | None) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    if dockerfile is None:
        (root / "Dockerfile").unlink()
    else:
        write_text(root / "Dockerfile", dockerfile)
    assert outcome_for(root, "docker-secret").status == parity.FAIL


def test_docker_secret_handles_continuations_and_comments(tmp_path: Path) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    write_text(
        root / "Dockerfile",
        GOLDEN_DOCKERFILE
        + (
            'RUN echo "uv sync does not run here"\n'
            "RUN echo okay # uv sync is only a comment\n"
            "RUN --mount=type=secret,id=github_token \\\n"
            "    # an explanatory comment\n"
            "    if true; then uv \\\n"
            "      sync --no-cache; fi\n"
        ),
    )
    assert outcome_for(root, "docker-secret").status == parity.PASS


def test_docker_secret_accepts_json_run_with_other_flags(tmp_path: Path) -> None:
    root = write_golden(tmp_path / GOLDEN_NAME)
    write_text(
        root / "Dockerfile",
        (
            "# syntax=docker/dockerfile:1.7\n"
            'RUN --network=host --mount=type=secret,id=github_token ["uv", "sync", "--no-dev"]\n'
        ),
    )
    assert outcome_for(root, "docker-secret").status == parity.PASS
