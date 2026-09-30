#!/usr/bin/env python3
"""Run the family compose commands consistently on Windows, macOS and Linux."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

Run = Callable[..., subprocess.CompletedProcess[str]]

ROOT = Path(__file__).resolve().parents[1]
LOCAL_OVERLAY = "docker-compose.local.yml"
ACTIONS: dict[str, tuple[str, ...]] = {
    "build": ("build",),
    "up": ("up", "-d", "--no-build"),
    "down": ("down",),
}


class ComposeError(Exception):
    """A safe operator-facing failure."""


def command(action: str, root: Path = ROOT) -> list[str]:
    """Build one compose command, including the private overlay when present."""
    args = ["docker", "compose"]
    overlay = root / LOCAL_OVERLAY
    if overlay.is_file():
        args.extend(("-f", "docker-compose.yml", "-f", LOCAL_OVERLAY))
    args.extend(ACTIONS[action])
    return args


def environment(
    action: str,
    source: Mapping[str, str] = os.environ,
    *,
    run: Run = subprocess.run,
) -> dict[str, str]:
    """Give the GitHub credential only to an image build."""
    env = dict(source)
    env.pop("GITHUB_TOKEN", None)
    if action != "build":
        return env
    completed = run(
        ["gh", "auth", "token"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ComposeError("GitHub sign-in is required for family image builds; run gh auth login")
    env["GITHUB_TOKEN"] = completed.stdout.strip()
    return env


def execute(
    action: str,
    *,
    root: Path = ROOT,
    source: Mapping[str, str] = os.environ,
    run: Run = subprocess.run,
) -> int:
    """Run an action without ever putting its credential in command text."""
    env = environment(action, source, run=run)
    completed = run(command(action, root), cwd=root, env=env, check=False)
    return int(completed.returncode)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("action", choices=tuple(ACTIONS))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return execute(args.action)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
