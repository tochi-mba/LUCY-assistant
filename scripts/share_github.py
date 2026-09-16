#!/usr/bin/env python3
"""Copy a browser GitHub login into Actions. Never create a personal access token.

Usage::

    python scripts/share_github.py            # install FAMILY_GITHUB_TOKEN on all nine
    python scripts/share_github.py --dry-run  # print the plan; change nothing

Sign in with ``gh auth login --web``. GitHub Actions cannot open a browser, so CI
receives that same login as a repository secret. This script never prints the
credential, never writes it to a file, and never puts it on a command line.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

META_ROOT = Path(__file__).resolve().parents[1]
META_NAME = "LUCY-assistant"
SECRET_NAME = "FAMILY_GITHUB_TOKEN"
LOGIN = (
    "gh",
    "auth",
    "login",
    "--hostname",
    "github.com",
    "--git-protocol",
    "https",
    "--web",
)
Run = Callable[..., subprocess.CompletedProcess[str]]


class ShareError(Exception):
    """A user-facing failure; the message is already safe to print."""


def read_family(repos_file: Path) -> tuple[str, list[str]]:
    """Owner plus repository names, meta first, from ``repos.txt``."""
    try:
        text = repos_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ShareError(f"cannot read {repos_file.name}: {exc.strerror}") from exc
    owner = ""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or "github.com/" not in parts[1]:
            raise ShareError(f"{repos_file.name}: {line!r} is not '<folder> <https clone URL>'")
        folder, url = parts[0], parts[1]
        _, _, rest = url.partition("github.com/")
        if "github.com/" not in url or not rest:
            raise ShareError(f"{repos_file.name}: {url} is not a github.com URL")
        found_owner, _, repo = rest.strip("/").partition("/")
        repo = repo.removesuffix(".git")
        if not found_owner or repo != folder:
            raise ShareError(
                f"{repos_file.name}: {url} must be https://github.com/<owner>/{folder}.git"
            )
        if owner and found_owner != owner:
            raise ShareError(f"{repos_file.name}: mixed owners {owner!r} and {found_owner!r}")
        owner = found_owner
        names.append(folder)
    if not owner:
        raise ShareError(f"{repos_file.name} lists no repositories")
    return owner, [META_NAME, *names]


def targets(repos_file: Path) -> list[str]:
    owner, names = read_family(repos_file)
    return [f"{owner}/{name}" for name in names]


def _run(
    run: Run,
    args: Sequence[str],
    *,
    input: str | None = None,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return run(
        list(args),
        input=input,
        capture_output=capture,
        text=True,
        check=False,
        timeout=120,
    )


def ensure_login(*, dry_run: bool, interactive: bool, run: Run) -> None:
    status = _run(run, ("gh", "auth", "status", "--hostname", "github.com"))
    if status.returncode == 0:
        return
    if dry_run:
        print(f"dry-run: {' '.join(LOGIN)}")
        return
    if not interactive:
        raise ShareError(
            "not signed in; run gh auth login --hostname github.com --git-protocol https --web"
        )
    print("Sign in to GitHub in your browser so Actions can read the family's private repositories")
    login = _run(run, LOGIN, capture=False)
    if login.returncode != 0:
        raise ShareError("GitHub sign-in did not complete")
    confirm = _run(run, ("gh", "auth", "status", "--hostname", "github.com"))
    if confirm.returncode != 0:
        raise ShareError("GitHub sign-in did not complete")


def session_token(run: Run) -> str:
    result = _run(run, ("gh", "auth", "token", "--hostname", "github.com"))
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        raise ShareError("could not read the signed-in GitHub session")
    return token


def install_secret(repo: str, token: str, *, run: Run) -> None:
    result = _run(
        run,
        ("gh", "secret", "set", SECRET_NAME, "--repo", repo),
        input=token,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "secret set failed").strip().splitlines()
        message = err[0] if err else "secret set failed"
        if token in message:
            message = "secret set failed"
        raise ShareError(f"{repo}: {message}")


def share(*, repos_file: Path, dry_run: bool, interactive: bool, run: Run) -> int:
    try:
        repos = targets(repos_file)
        ensure_login(dry_run=dry_run, interactive=interactive, run=run)
    except ShareError as exc:
        print(f"share-github: {exc}", file=sys.stderr)
        return 2
    if dry_run:
        print(f"dry-run: would set {SECRET_NAME} on {len(repos)} repositories from your browser login")
        for repo in repos:
            print(f"dry-run: gh secret set {SECRET_NAME} --repo {repo}")
        return 0
    try:
        token = session_token(run)
        for repo in repos:
            install_secret(repo, token, run=run)
            print(f"{repo}: {SECRET_NAME} updated")
    except ShareError as exc:
        print(f"share-github: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {SECRET_NAME} on {len(repos)} repositories from your browser login.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy a browser GitHub login into Actions. Never creates a PAT.",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument(
        "--repos-file",
        type=Path,
        default=META_ROOT / "repos.txt",
        help="family manifest (default: repos.txt)",
    )
    return parser


def main(argv: list[str] | None = None, *, run: Run = subprocess.run) -> int:
    args = _parser().parse_args(argv)
    repos_file = args.repos_file if args.repos_file.is_absolute() else Path.cwd() / args.repos_file
    return share(
        repos_file=repos_file,
        dry_run=args.dry_run,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        run=run,
    )


if __name__ == "__main__":
    sys.exit(main())
