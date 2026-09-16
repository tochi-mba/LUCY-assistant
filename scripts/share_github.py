#!/usr/bin/env python3
"""Install the family's read-only GitHub token as the FAMILY_GITHUB_TOKEN Actions secret.

Usage::

    python scripts/share_github.py            # asks for the token (hidden), installs it on all nine
    python scripts/share_github.py --dry-run  # checks the token and prints the plan; sets nothing
    python scripts/share_github.py --stdin    # reads the token from stdin (a password manager)

GitHub Actions cannot open a browser, so CI authenticates with a **fine-grained personal
access token** limited to the family repositories with *Contents: read-only*. This script
checks that the token can read every repository in ``repos.txt`` plus this meta-repo, then
sets it as a secret on each of them with your own ``gh`` login. The token travels on
standard input and in the environment, never on a command line, and is never printed.

It refuses a classic token or a ``gh`` OAuth session (``ghp_``/``gho_``): those can write
to every repository on the account, and CI only needs to read nine.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

META_ROOT = Path(__file__).resolve().parents[1]
META_NAME = "LUCY-assistant"
SECRET_NAME = "FAMILY_GITHUB_TOKEN"  # noqa: S105 - the secret's name, not a value
FINE_GRAINED_PREFIX = "github_pat_"
NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"  # noqa: S105 - a page
LOGIN = ("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https")
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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        list(args),
        input=input,
        capture_output=capture,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )


def ensure_login(*, interactive: bool, run: Run) -> None:
    """Your own ``gh`` login sets the secrets; the family token is never used for that."""
    if _run(run, ("gh", "auth", "status", "--hostname", "github.com")).returncode == 0:
        return
    if not interactive:
        raise ShareError("not signed in to GitHub; run " + " ".join(LOGIN))
    print("Sign in to GitHub (browser or a pasted token) so this script can set repository secrets")
    if _run(run, LOGIN, capture=False).returncode != 0:
        raise ShareError("GitHub sign-in did not complete")
    if _run(run, ("gh", "auth", "status", "--hostname", "github.com")).returncode != 0:
        raise ShareError("GitHub sign-in did not complete")


def read_token(*, from_stdin: bool, interactive: bool, environ: dict[str, str]) -> str:
    """The token from stdin, the environment, or a hidden prompt; never from an argument."""
    if from_stdin:
        token = sys.stdin.readline().strip()
        source = "standard input"
    elif environ.get(SECRET_NAME):
        token = environ[SECRET_NAME].strip()
        source = f"${SECRET_NAME}"
    elif interactive:
        print(f"Create a fine-grained token at {NEW_TOKEN_URL}")
        print(
            "  Repository access: only the family repositories.  Permissions: Contents, read-only."
        )
        token = getpass.getpass("Paste the token (not shown): ").strip()
        source = "the prompt"
    else:
        raise ShareError(
            f"no token: export {SECRET_NAME}, pipe it with --stdin, or run from a terminal"
        )
    if not token:
        raise ShareError(f"no token was read from {source}")
    return token


def check_token_shape(token: str, *, allow_any: bool) -> None:
    if token.startswith(FINE_GRAINED_PREFIX) or allow_any:
        return
    raise ShareError(
        "that is a classic token or a gh session (ghp_/gho_), which can write to every "
        "repository on the account; CI only needs to read the family. Create a fine-grained "
        f"token at {NEW_TOKEN_URL} (Contents: read-only, only the family repositories), "
        "or pass --allow-any-token if you have a reason"
    )


def unreadable(repos: Sequence[str], token: str, *, run: Run) -> list[str]:
    """Repositories the token cannot read, checked with the token itself (via GH_TOKEN)."""
    env = {**os.environ, "GH_TOKEN": token}
    return [
        repo
        for repo in repos
        if _run(run, ("gh", "api", f"repos/{repo}", "--silent"), env=env).returncode != 0
    ]


def install_secret(repo: str, token: str, *, run: Run) -> None:
    result = _run(run, ("gh", "secret", "set", SECRET_NAME, "--repo", repo), input=token)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "secret set failed").strip().splitlines()
        message = err[0] if err else "secret set failed"
        if token in message:
            message = "secret set failed"
        raise ShareError(f"{repo}: {message}")


def share(
    *,
    repos_file: Path,
    dry_run: bool,
    interactive: bool,
    from_stdin: bool,
    allow_any: bool,
    run: Run,
    environ: dict[str, str] | None = None,
) -> int:
    try:
        repos = targets(repos_file)
        ensure_login(interactive=interactive, run=run)
        token = read_token(
            from_stdin=from_stdin,
            interactive=interactive,
            environ=os.environ if environ is None else environ,
        )
        check_token_shape(token, allow_any=allow_any)
    except ShareError as exc:
        print(f"share-github: {exc}", file=sys.stderr)
        return 2
    denied = unreadable(repos, token, run=run)
    if denied:
        print(
            "share-github: the token cannot read "
            + ", ".join(denied)
            + "; add each to the token's repository list, then run this again",
            file=sys.stderr,
        )
        return 1
    print(f"The token can read all {len(repos)} family repositories.")
    if dry_run:
        for repo in repos:
            print(f"dry-run: gh secret set {SECRET_NAME} --repo {repo}")
        print("dry-run: nothing was changed")
        return 0
    try:
        for repo in repos:
            install_secret(repo, token, run=run)
            print(f"{repo}: {SECRET_NAME} updated")
    except ShareError as exc:
        print(f"share-github: {exc}", file=sys.stderr)
        return 1
    print(f"Installed {SECRET_NAME} on {len(repos)} repositories. Run this again to rotate it.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install the family's read-only fine-grained token as the "
            f"{SECRET_NAME} Actions secret on every family repository."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="check the token and print the plan; set nothing"
    )
    parser.add_argument(
        "--stdin", action="store_true", help="read the token from standard input (one line)"
    )
    parser.add_argument(
        "--allow-any-token",
        action="store_true",
        help="accept a token that is not fine-grained (github_pat_...)",
    )
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
        interactive=sys.stdin.isatty() and sys.stdout.isatty() and not args.stdin,
        from_stdin=args.stdin,
        allow_any=args.allow_any_token,
        run=run,
    )


if __name__ == "__main__":
    sys.exit(main())
