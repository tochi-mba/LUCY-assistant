#!/usr/bin/env python3
"""Check each LUCY-assistant service repository against the family standard.

Usage::

    python scripts/parity.py                   # every repository in repos.txt
    python scripts/parity.py --repo User-api   # one repository
    python scripts/parity.py --json            # the same results, as JSON

The standard is the one CONTRIBUTING.md describes: the Makefile targets, the tool
configuration in pyproject.toml, the documentation set, the configuration convention, the
health routes, and token verification through ``keyring_client``.

Standard library only, so it runs before any repository has a virtualenv. It reads files
and changes nothing. Exit status: 0 when every check passes, 1 when any fails, 2 for a
usage error.

Adding a check is one entry in ``CHECKS``: a function that takes a :class:`Repo` and
returns a :class:`Result`. ``tests/test_parity.py`` refuses a check that has no failing
case.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    sys.exit("parity.py needs Python 3.11 or newer: it reads pyproject.toml with tomllib")

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

META_ROOT = Path(__file__).resolve().parent.parent
REPOS_FILE = META_ROOT / "repos.txt"

KEYRING_REPO = "Keyring-api"
"""Issues the tokens the other seven verify, so it is the one repository that does not
consume them through ``keyring_client``."""

REQUIRED_MAKE_TARGETS = (
    "help",
    "install",
    "fmt",
    "lint",
    "type",
    "imports",
    "test",
    "cov",
    "check",
    "run",
    "docker",
    "clean",
)
CHECK_PREREQUISITES = frozenset({"lint", "type", "imports", "test"})
PINNED_PYTHON = "3.11"
RUFF_LINE_LENGTH = 100
RUFF_TARGET = "py311"
MAX_FILE_LINES = 1000
"""No source, test, or script file in the family is allowed past this. Split the module."""
DOCUMENTATION_SET = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs/architecture.md",
    "docs/api.md",
    "docs/operations.md",
    "docs/testing.md",
    "docs/adr/README.md",
)
CONFIG_MODULES = (
    "src/*/core/config.py",
    "src/*/config.py",
    "app/core/config.py",
    "app/config.py",
    "app/settings.py",
)
"""Where each layout keeps its settings module, in the order they are looked for."""

HEALTH_ROUTES = ("/healthy", "/ready")

PASS = "pass"
FAIL = "fail"
NOT_APPLICABLE = "n/a"

_TARGET_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)[ \t]*:(?![:=])(?P<rest>.*)$")
_PRAGMA = re.compile(r"pragma:\s*no\s*cover")
_KEYRING_IMPORT = re.compile(r"^\s*(?:from|import)\s+keyring_client\b", re.MULTILINE)
_KEYRING_DEPENDENCY = re.compile(r"^\s*keyring[-_.]client\b", re.IGNORECASE)
_ENV_PREFIX = re.compile(r"\benv_prefix\s*=")
_EXTRA_FORBID = re.compile(r"""\bextra\s*=\s*["']forbid["']""")


class ParityError(Exception):
    """A file a check needs is missing, or cannot be read as what it claims to be."""


@dataclass(frozen=True)
class Result:
    """One check's verdict on one repository."""

    status: str
    detail: str = ""


def passed(detail: str = "") -> Result:
    return Result(PASS, detail)


def failed(detail: str) -> Result:
    return Result(FAIL, detail)


def not_applicable(detail: str) -> Result:
    return Result(NOT_APPLICABLE, detail)


def parse_makefile(text: str) -> dict[str, list[str]]:
    """Map each explicit target to its prerequisites.

    Recipe lines, variable assignments (``UV := uv``) and special targets (``.PHONY``) are
    ignored. Good enough for the family's Makefiles, which are flat on purpose.
    """
    targets: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = _TARGET_LINE.match(line)
        if match is None:
            continue
        rest = match.group("rest").split("#", 1)[0]
        if "=" in rest:  # a target-specific variable: `target: VAR = value`
            continue
        targets.setdefault(match.group("name"), []).extend(rest.replace("|", " ").split())
    return targets


def dig(data: dict[str, Any], *keys: str) -> Any:
    """``data[k1][k2]...``, or ``None`` as soon as a key is absent."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _describe(value: object) -> str:
    return "not set" if value is None else repr(value)


@dataclass
class Repo:
    """A checked-out service repository, read lazily."""

    name: str
    path: Path

    @property
    def issues_tokens(self) -> bool:
        return self.name == KEYRING_REPO

    def exists(self, relative: str) -> bool:
        return (self.path / relative).is_file()

    def read(self, relative: str) -> str | None:
        target = self.path / relative
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def relative(self, file: Path) -> str:
        return file.relative_to(self.path).as_posix()

    @cached_property
    def makefile(self) -> dict[str, list[str]] | None:
        text = self.read("Makefile")
        return None if text is None else parse_makefile(text)

    @cached_property
    def pyproject(self) -> dict[str, Any] | None:
        text = self.read("pyproject.toml")
        if text is None:
            return None
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ParityError(f"pyproject.toml is not valid TOML: {exc}") from None

    @cached_property
    def source_dirs(self) -> tuple[Path, ...]:
        """``src/<package>/`` for the src layout, otherwise ``app/``."""
        src = self.path / "src"
        if src.is_dir():
            packages = tuple(
                sorted(child for child in src.iterdir() if (child / "__init__.py").is_file())
            )
            if packages:
                return packages
        app = self.path / "app"
        return (app,) if app.is_dir() else ()

    @cached_property
    def config_module(self) -> Path | None:
        for pattern in CONFIG_MODULES:
            matches = sorted(self.path.glob(pattern))
            if matches:
                return matches[0]
        return None

    def python_files(self) -> Iterator[Path]:
        for directory in self.source_dirs:
            for file in sorted(directory.rglob("*.py")):
                if "__pycache__" not in file.parts:
                    yield file

    def code_files(self) -> Iterator[Path]:
        """Python under the source package, tests/, scripts/, and clients/."""
        seen: set[Path] = set()
        roots = list(self.source_dirs)
        for relative in ("tests", "scripts", "clients"):
            directory = self.path / relative
            if directory.is_dir():
                roots.append(directory)
        for directory in roots:
            for file in sorted(directory.rglob("*.py")):
                if "__pycache__" in file.parts or file in seen:
                    continue
                seen.add(file)
                yield file


def _makefile(repo: Repo) -> dict[str, list[str]]:
    if repo.makefile is None:
        raise ParityError("Makefile is missing")
    return repo.makefile


def _pyproject(repo: Repo) -> dict[str, Any]:
    data = repo.pyproject
    if data is None:
        raise ParityError("pyproject.toml is missing")
    return data


def _config_source(repo: Repo) -> tuple[str, str]:
    module = repo.config_module
    if module is None:
        raise ParityError("no config module (looked for " + ", ".join(CONFIG_MODULES) + ")")
    return repo.relative(module), module.read_text(encoding="utf-8", errors="replace")


def _require_source(repo: Repo) -> None:
    if not repo.source_dirs:
        raise ParityError("no source package (src/<package>/ or app/)")


# -- Checks ------------------------------------------------------------------------------


def check_make_targets(repo: Repo) -> Result:
    """Every repository answers to the same verbs, so moving between them costs nothing."""
    targets = _makefile(repo)
    missing = [name for name in REQUIRED_MAKE_TARGETS if name not in targets]
    return failed("missing target(s): " + " ".join(missing)) if missing else passed()


def check_make_check(repo: Repo) -> Result:
    """`make check` is the gate, and it is the same four gates everywhere."""
    targets = _makefile(repo)
    if "check" not in targets:
        return failed("no check target")
    runs = targets["check"]
    if set(runs) != CHECK_PREREQUISITES or len(runs) != len(CHECK_PREREQUISITES):
        return failed(
            f"check runs: {' '.join(runs) or '(nothing)'}; expected: lint type imports test"
        )
    return passed()


def check_python_version(repo: Repo) -> Result:
    text = repo.read(".python-version")
    if text is None:
        return failed(".python-version is missing")
    pinned = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if pinned == PINNED_PYTHON or pinned.startswith(PINNED_PYTHON + "."):
        return passed(pinned)
    return failed(f".python-version pins {pinned or '(nothing)'}; expected {PINNED_PYTHON}")


def check_claude_md(repo: Repo) -> Result:
    text = repo.read("CLAUDE.md")
    if text is None:
        return failed("CLAUDE.md is missing")
    if "AGENTS.md" not in text:
        return failed("CLAUDE.md does not point at AGENTS.md")
    return passed()


def check_documentation(repo: Repo) -> Result:
    missing = [path for path in DOCUMENTATION_SET if not repo.exists(path)]
    return failed("missing: " + ", ".join(missing)) if missing else passed()


def check_changelog(repo: Repo) -> Result:
    text = repo.read("CHANGELOG.md")
    if text is None:
        return failed("CHANGELOG.md is missing")
    if "keepachangelog.com" not in text.lower():
        return failed("CHANGELOG.md does not declare the Keep a Changelog format")
    return passed()


def check_pre_commit(repo: Repo) -> Result:
    if repo.exists(".pre-commit-config.yaml"):
        return passed()
    return failed(".pre-commit-config.yaml is missing")


def check_editorconfig(repo: Repo) -> Result:
    return passed() if repo.exists(".editorconfig") else failed(".editorconfig is missing")


def check_ci_secrets(repo: Repo) -> Result:
    """The job calling the family workflow must pass its repository's secrets."""
    source = repo.read(".github/workflows/ci.yml")
    if source is None:
        return failed(".github/workflows/ci.yml is missing")
    # The family uses block mappings. Track their indentation so inheritance on an
    # unrelated job, a step, a comment, or a multiline string cannot satisfy this check.
    stack: list[tuple[int, str]] = []
    jobs: dict[str, dict[str, str]] = {}
    for line in source.splitlines():
        match = re.fullmatch(r"( *)([A-Za-z0-9_-]+):(?:[ \t]+(.*))?", line)
        if match is None:
            continue
        indent, key = len(match[1]), match[2]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = [item[1] for item in stack] + [key]
        if len(path) == 3 and path[0] == "jobs":
            value = re.sub(r"\s+#.*$", "", match[3] or "").strip().strip("'\"")
            jobs.setdefault(path[1], {})[key] = value
        stack.append((indent, key))
    callers = {
        name: job
        for name, job in jobs.items()
        if re.fullmatch(
            r"[A-Za-z0-9-]+/LUCY-assistant/\.github/workflows/service\.yml@[^\s]+",
            job.get("uses", ""),
        )
    }
    if not callers:
        return failed("ci.yml has no job calling the family reusable workflow")
    missing = [name for name, job in callers.items() if job.get("secrets") != "inherit"]
    return (
        failed("missing secrets: inherit on job(s): " + ", ".join(missing)) if missing else passed()
    )


def _docker_instructions(source: str) -> Iterator[tuple[int, str]]:
    """Join Docker's continued instruction lines, ignoring full-line comments."""
    pending = ""
    start = 0
    for number, line in enumerate(source.splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if not pending:
            start = number
        continued = text.endswith("\\")
        pending += (text[:-1] if continued else text) + " "
        if not continued:
            yield start, pending.strip()
            pending = ""
    if pending:
        yield start, pending.strip()


def check_docker_secret(repo: Repo) -> Result:
    """Every uv sync RUN receives its own BuildKit token mount, including later layers."""
    source = repo.read("Dockerfile")
    if source is None:
        return failed("Dockerfile is missing")
    lines = source.splitlines()
    if not lines or not re.fullmatch(r"#\s*syntax=docker/dockerfile:1(?:[.\w-]*)\s*", lines[0]):
        return failed("Dockerfile line 1 must declare # syntax=docker/dockerfile:1")
    count = 0
    unprotected = []
    for number, instruction in _docker_instructions(source):
        match = re.match(r"RUN\s+(.*)", instruction, re.IGNORECASE)
        if match is None:
            continue
        command = match[1]
        mounted = False
        while flag := re.match(r"--([\w-]+)=([^\s]+)\s+", command):
            if flag[1] == "mount":
                options = dict(part.split("=", 1) for part in flag[2].split(",") if "=" in part)
                mounted |= options.get("type") == "secret" and options.get("id") == "github_token"
            command = command[flag.end() :]
        try:
            if command.startswith("["):
                words = json.loads(command)
            else:
                lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
                lexer.whitespace_split = True
                words = list(lexer)
        except (ValueError, TypeError) as exc:
            return failed(f"Dockerfile:{number}: cannot read RUN: {exc}")
        if any(first == "uv" and second == "sync" for first, second in zip(words, words[1:])):
            count += 1
            if not mounted:
                unprotected.append(str(number))
    if unprotected:
        return failed(
            "uv sync RUN missing github_token secret mount at line(s): " + ", ".join(unprotected)
        )
    return (
        passed(f"{count} protected uv sync RUN(s)")
        if count
        else failed("Dockerfile has no uv sync RUN")
    )


def check_dev_group(repo: Repo) -> Result:
    data = _pyproject(repo)
    if dig(data, "project", "optional-dependencies", "dev") is not None:
        return failed(
            "dev dependencies are an extra ([project.optional-dependencies] dev), "
            "not [dependency-groups] dev"
        )
    if not dig(data, "dependency-groups", "dev"):
        return failed("no [dependency-groups] dev")
    return passed()


def check_ruff(repo: Repo) -> Result:
    data = _pyproject(repo)
    problems = []
    length = dig(data, "tool", "ruff", "line-length")
    if length != RUFF_LINE_LENGTH:
        problems.append(f"line-length is {_describe(length)}")
    target = dig(data, "tool", "ruff", "target-version")
    if target != RUFF_TARGET:
        problems.append(f"target-version is {_describe(target)}")
    return failed("[tool.ruff] " + "; ".join(problems)) if problems else passed()


def check_mypy_strict(repo: Repo) -> Result:
    value = dig(_pyproject(repo), "tool", "mypy", "strict")
    return passed() if value is True else failed(f"[tool.mypy] strict is {_describe(value)}")


def check_coverage(repo: Repo) -> Result:
    data = _pyproject(repo)
    problems = []
    fail_under = dig(data, "tool", "coverage", "report", "fail_under")
    if fail_under != 100:  # noqa: PLR2004 -- the number is the rule
        problems.append(f"[tool.coverage.report] fail_under is {_describe(fail_under)}")
    branch = dig(data, "tool", "coverage", "run", "branch")
    if branch is not True:
        problems.append(f"[tool.coverage.run] branch is {_describe(branch)}")
    return failed("; ".join(problems)) if problems else passed()


def check_pytest_warnings(repo: Repo) -> Result:
    """A new warning fails the suite, rather than scrolling past."""
    value = dig(_pyproject(repo), "tool", "pytest", "ini_options", "filterwarnings")
    entries = [value] if isinstance(value, str) else list(value or [])
    if any(isinstance(entry, str) and entry.strip() == "error" for entry in entries):
        return passed()
    if not entries:
        return failed("[tool.pytest.ini_options] filterwarnings is not set")
    return failed('filterwarnings has no plain "error" entry: ' + ", ".join(map(str, entries)))


def check_import_linter(repo: Repo) -> Result:
    contracts = dig(_pyproject(repo), "tool", "importlinter", "contracts")
    if not contracts:
        return failed("no [[tool.importlinter.contracts]]")
    return passed(f"{len(contracts)} contract(s)")


def check_config(repo: Repo) -> Result:
    relative, source = _config_source(repo)
    problems = []
    if not _ENV_PREFIX.search(source):
        problems.append("no env_prefix")
    if not _EXTRA_FORBID.search(source):
        problems.append('no extra="forbid"')
    return failed(f"{relative}: " + "; ".join(problems)) if problems else passed(relative)


def check_unknown_env(repo: Repo) -> Result:
    """pydantic-settings ignores a misspelled variable; the family refuses to start instead."""
    relative, source = _config_source(repo)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return failed(f"{relative} does not parse (line {exc.lineno})")
    defined = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    if "check_for_unknown_env_vars" in defined:
        return passed(relative)
    return failed(f"{relative} defines no check_for_unknown_env_vars")


def check_no_pragma(repo: Repo) -> Result:
    """100% coverage means every line is tested, not every untested line is excused."""
    _require_source(repo)
    hits = []
    for file in repo.python_files():
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        hits.extend(
            f"{repo.relative(file)}:{number}"
            for number, line in enumerate(lines, start=1)
            if _PRAGMA.search(line)
        )
    if hits:
        more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""  # noqa: PLR2004
        return failed(f"{len(hits)} found: {', '.join(hits[:3])}{more}")
    return passed()


def check_health_routes(repo: Repo) -> Result:
    _require_source(repo)
    source = "\n".join(
        file.read_text(encoding="utf-8", errors="replace") for file in repo.python_files()
    )
    missing = [
        route
        for route in HEALTH_ROUTES
        if not re.search(rf"""["']{re.escape(route)}["']""", source)
    ]
    return failed("no " + " or ".join(missing) + " route found") if missing else passed()


def _line_count(text: str) -> int:
    """Physical lines, counting a last line even when it has no trailing newline."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def check_max_file_lines(repo: Repo) -> Result:
    """A file past a thousand lines is a module that nobody can hold in their head."""
    _require_source(repo)
    hits = []
    for file in repo.code_files():
        lines = _line_count(file.read_text(encoding="utf-8", errors="replace"))
        if lines > MAX_FILE_LINES:
            hits.append(f"{repo.relative(file)}:{lines}")
    if hits:
        more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""  # noqa: PLR2004
        return failed(f"{len(hits)} over {MAX_FILE_LINES}: {', '.join(hits[:3])}{more}")
    return passed()


def check_keyring_client(repo: Repo) -> Result:
    """One library, one set of rules for believing a token."""
    if repo.issues_tokens:
        return not_applicable("issues the tokens the others verify")
    dependencies = dig(_pyproject(repo), "project", "dependencies") or []
    _require_source(repo)
    problems = []
    if not any(isinstance(dep, str) and _KEYRING_DEPENDENCY.match(dep) for dep in dependencies):
        problems.append("keyring-client is not a project dependency")
    if not any(
        _KEYRING_IMPORT.search(file.read_text(encoding="utf-8", errors="replace"))
        for file in repo.python_files()
    ):
        problems.append("nothing in the source package imports keyring_client")
    return failed("; ".join(problems)) if problems else passed()


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    run: Callable[[Repo], Result]


CHECKS: tuple[Check, ...] = (
    Check("make-targets", "Makefile has the family targets", check_make_targets),
    Check("make-check", "make check runs lint type imports test", check_make_check),
    Check("python-version", ".python-version pins 3.11", check_python_version),
    Check("claude-md", "CLAUDE.md points at AGENTS.md", check_claude_md),
    Check("docs", "documentation set is present", check_documentation),
    Check("changelog", "CHANGELOG.md follows Keep a Changelog", check_changelog),
    Check("pre-commit", ".pre-commit-config.yaml exists", check_pre_commit),
    Check("editorconfig", ".editorconfig exists", check_editorconfig),
    Check("ci-secrets", "family workflow caller inherits secrets", check_ci_secrets),
    Check("docker-secret", "every uv sync RUN mounts the GitHub token secret", check_docker_secret),
    Check("dev-group", "dev dependencies in [dependency-groups] dev", check_dev_group),
    Check("ruff", "ruff line-length 100, target py311", check_ruff),
    Check("mypy-strict", "mypy strict = true", check_mypy_strict),
    Check("coverage", "branch coverage, fail_under = 100", check_coverage),
    Check("pytest-warnings", 'pytest filterwarnings has "error"', check_pytest_warnings),
    Check("import-linter", "import-linter contracts declared", check_import_linter),
    Check("config", 'config has an env prefix and extra="forbid"', check_config),
    Check("unknown-env", "config defines check_for_unknown_env_vars", check_unknown_env),
    Check("no-pragma", "no pragma: no cover in the source package", check_no_pragma),
    Check("max-file-lines", "no code file over 1000 lines", check_max_file_lines),
    Check("health-routes", "serves /healthy and /ready", check_health_routes),
    Check("keyring-client", "verifies tokens with keyring_client", check_keyring_client),
)


def run_check(check: Check, repo: Repo) -> Result:
    try:
        return check.run(repo)
    except (ParityError, OSError) as exc:
        return failed(str(exc))


# -- Reporting ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    check: Check
    result: Result


@dataclass(frozen=True)
class RepoReport:
    name: str
    present: bool
    outcomes: tuple[Outcome, ...]

    @property
    def ok(self) -> bool:
        return self.present and all(o.result.status != FAIL for o in self.outcomes)


def read_repo_names(repos_file: Path) -> list[str]:
    """Names from ``repos.txt``: ``<name> <url>`` per line; blank lines and ``#`` comments skipped."""
    names = []
    for raw in repos_file.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.append(line.split()[0])
    return names


def evaluate(root: Path, names: Sequence[str]) -> list[RepoReport]:
    reports = []
    for name in names:
        path = root / name
        if not path.is_dir():
            reports.append(RepoReport(name, present=False, outcomes=()))
            continue
        repo = Repo(name, path)
        outcomes = tuple(Outcome(check, run_check(check, repo)) for check in CHECKS)
        reports.append(RepoReport(name, present=True, outcomes=outcomes))
    return reports


_LABELS = {PASS: "pass", FAIL: "FAIL", NOT_APPLICABLE: "n/a"}
_ABSENT = "missing"


def short_name(name: str) -> str:
    for suffix in ("-api", "-tool"):
        if name.lower().endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _join(cells: Sequence[str], widths: Sequence[int]) -> str:
    return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip()


def render_text(reports: Sequence[RepoReport]) -> str:
    if len(reports) == 1:
        return _render_one(reports[0])
    headers = ["check", *(short_name(report.name) for report in reports)]
    widths = [max(len(c.id) for c in CHECKS), *(max(len(h), len(_ABSENT)) for h in headers[1:])]
    lines = [_join(headers, widths), _join(["-" * width for width in widths], widths)]
    for index, check in enumerate(CHECKS):
        cells = [check.id]
        for report in reports:
            status = report.outcomes[index].result.status if report.present else None
            cells.append(_ABSENT if status is None else _LABELS[status])
        lines.append(_join(cells, widths))
    problems = [f"  {r.name}: not checked out under the root" for r in reports if not r.present]
    problems += [
        f"  {report.name} / {outcome.check.id}: {outcome.result.detail}"
        for report in reports
        for outcome in report.outcomes
        if outcome.result.status == FAIL
    ]
    if problems:
        lines += ["", "Failures:", *problems]
    passing = sum(report.ok for report in reports)
    lines += ["", f"{passing} of {len(reports)} repositories pass every check."]
    return "\n".join(lines)


def _render_one(report: RepoReport) -> str:
    if not report.present:
        return f"{report.name}: not checked out under the root"
    widths = [max(len(c.id) for c in CHECKS), len("result")]
    lines = [
        _join(["check", "result", "detail"], [*widths, 0]),
        _join(["-" * widths[0], "-" * widths[1], "------"], [*widths, 0]),
    ]
    lines += [
        _join([o.check.id, _LABELS[o.result.status], o.result.detail], [*widths, 0])
        for o in report.outcomes
    ]
    verdict = "passes every check" if report.ok else "has drifted from the family standard"
    lines += ["", f"{report.name} {verdict}."]
    return "\n".join(lines)


def render_json(reports: Sequence[RepoReport], root: Path) -> str:
    document = {
        "root": str(root),
        "ok": all(report.ok for report in reports),
        "repos": [
            {
                "name": report.name,
                "present": report.present,
                "ok": report.ok,
                "checks": [
                    {
                        "id": outcome.check.id,
                        "title": outcome.check.title,
                        "status": outcome.result.status,
                        "detail": outcome.result.detail,
                    }
                    for outcome in report.outcomes
                ],
            }
            for report in reports
        ],
    }
    return json.dumps(document, indent=2)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check LUCY-assistant service repositories against the family standard.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        metavar="NAME",
        help="check only this repository (repeatable); default: every one in repos.txt",
    )
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    parser.add_argument(
        "--root",
        type=Path,
        default=META_ROOT,
        help="folder holding the repository checkouts (default: this repository's root)",
    )
    parser.add_argument(
        "--repos-file",
        type=Path,
        default=REPOS_FILE,
        help="the repository manifest (default: repos.txt beside scripts/)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve()
    try:
        known = read_repo_names(args.repos_file)
    except OSError as exc:
        print(f"parity: cannot read {args.repos_file}: {exc.strerror}", file=sys.stderr)
        return 2
    names = args.repo or known
    unknown = [name for name in names if name not in known and not (root / name).is_dir()]
    if unknown:
        print(
            f"parity: unknown repository {', '.join(unknown)}; known: {', '.join(known)}",
            file=sys.stderr,
        )
        return 2
    reports = evaluate(root, names)
    print(render_json(reports, root) if args.json else render_text(reports))
    return 0 if all(report.ok for report in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
