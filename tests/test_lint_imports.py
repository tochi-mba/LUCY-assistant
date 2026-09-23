"""The import-linter runner translates a contract result into a shell exit status.

The script exists because three console-script shims are refused by an Application Control
policy on Windows; see its module docstring. What is worth testing is not import-linter --
that has its own suite -- but the two things this wrapper is responsible for: calling the
configuration hook that registers the option readers, and turning a ``bool`` into an exit
status, where passing the ``bool`` straight to :func:`sys.exit` would make success mean 1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import lint_imports as runner  # noqa: E402


def test_configure_runs_at_import() -> None:
    """Importing the runner must register the option readers that parse pyproject.toml.

    Without them the use case raises a bare ``'USER_OPTION_READERS'`` after printing its
    banner, so a piped ``make imports`` looks like a pass. Asserting the settings are
    populated is the cheap way to keep that from coming back.
    """
    from importlinter.application.app_config import settings

    readers = settings.USER_OPTION_READERS
    assert set(readers) == {"ini", "toml"}
    assert settings.GRAPH_BUILDER is not None


def test_kept_contracts_are_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "lint_imports", lambda **_: True)
    assert runner.main([]) == 0


def test_broken_contracts_are_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bool handed to `sys.exit` would invert this: `True` is exit status 1."""
    monkeypatch.setattr(runner, "lint_imports", lambda **_: False)
    assert runner.main([]) == 1


@pytest.mark.parametrize(("argv", "expected"), [([], False), (["--debug"], True)])
def test_debug_flag_reaches_the_use_case(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    seen: dict[str, Any] = {}

    def record(**kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(runner, "lint_imports", record)
    assert runner.main(argv) == 0
    assert seen["is_debug_mode"] is expected
    assert seen["config_filename"] is None


def test_argv_is_read_when_no_list_is_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make imports` passes nothing; a person debugging passes `--debug` on the command line."""
    seen: dict[str, Any] = {}

    def record(**kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(runner, "lint_imports", record)
    monkeypatch.setattr(sys, "argv", ["lint_imports.py", "--debug"])
    assert runner.main() == 0
    assert seen["is_debug_mode"] is True
