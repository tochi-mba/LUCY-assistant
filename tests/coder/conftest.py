"""Shared pieces: a store in tmp, a runner on the fake CLI, a service over both."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from lucy_coder.runner import ClaudeRunner
from lucy_coder.service import CoderService
from lucy_coder.tasks import TaskStore

if TYPE_CHECKING:
    from collections.abc import Iterator

FAKE = str(Path(__file__).with_name("fake_claude.py"))
ACCOUNT = "acct_coder"
STRANGER = "acct_other"


def fake_command() -> list[str]:
    return [sys.executable, FAKE]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TaskStore]:
    opened = TaskStore(str(tmp_path / "coder"))
    yield opened
    opened.close()


@pytest.fixture
def workdir(tmp_path: Path) -> str:
    place = tmp_path / "repo"
    place.mkdir()
    return str(place)


def a_runner(*, budget: float = 0.5, timeout: float = 30.0) -> ClaudeRunner:
    return ClaudeRunner(fake_command(), budget_usd=budget, timeout_seconds=timeout)


def a_service(store: TaskStore, *, max_live: int = 2, timeout: float = 30.0) -> CoderService:
    return CoderService(store, a_runner(timeout=timeout), max_live=max_live)
