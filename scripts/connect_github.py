#!/usr/bin/env python3
"""Install the shared, read-only family CI app from the browser.

Usage::

    python scripts/connect_github.py
    python scripts/connect_github.py --dry-run

The app's private key stays in the family token broker. This script never creates,
reads, writes, or prints a GitHub token or an Actions secret.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

META_ROOT = Path(__file__).resolve().parents[1]
META_NAME = "LUCY-assistant"
APP_FILE = META_ROOT / "family-app.json"
LOGIN = ("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https")
Run = Callable[..., subprocess.CompletedProcess[str]]


class ConnectError(Exception):
    """A user-facing failure whose message contains no credential."""


def _run(
    run: Run, args: Sequence[str], *, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return run(list(args), capture_output=capture, text=True, check=False, timeout=120)


def read_family(path: Path) -> tuple[str, list[str]]:
    """Return one owner and the repository names from ``repos.txt`` (+ local extras)."""
    paths = [path]
    for name in (".repos.local.txt", "repos.local.txt"):
        local = path.with_name(name)
        if local.is_file() and local.resolve() != path.resolve():
            paths.append(local)
            break
    owner = ""
    names = [META_NAME]
    seen: set[str] = {META_NAME}
    for manifest in paths:
        try:
            lines = manifest.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ConnectError(f"cannot read {manifest.name}: {exc.strerror}") from exc
        for number, raw in enumerate(lines, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            fields = line.split()
            if len(fields) != 2 or not fields[1].startswith("https://github.com/"):
                raise ConnectError(f"{manifest.name}:{number}: expected <folder> <https clone URL>")
            folder, url = fields
            if folder in seen:
                continue
            found_owner, separator, repository = url.removeprefix("https://github.com/").partition(
                "/"
            )
            repository = repository.removesuffix("/").removesuffix(".git")
            if not separator or repository != folder or not found_owner:
                raise ConnectError(f"{manifest.name}:{number}: URL must end in /{folder}.git")
            if owner and owner.casefold() != found_owner.casefold():
                raise ConnectError(
                    f"{manifest.name}:{number}: every repository must have one owner"
                )
            owner = found_owner
            seen.add(folder)
            names.append(folder)
    if not owner:
        raise ConnectError(f"{path.name} lists no repositories")
    return owner, names


def read_app(path: Path) -> tuple[str, str]:
    """Return the public app slug and client id."""
    try:
        body: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConnectError(f"cannot read {path.name}: {exc.strerror}") from exc
    except ValueError as exc:
        raise ConnectError(f"{path.name} is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ConnectError(f"{path.name} must be a JSON object")
    slug, client_id = body.get("slug"), body.get("client_id")
    if not isinstance(slug, str) or not slug or not isinstance(client_id, str) or not client_id:
        raise ConnectError(f"{path.name} must contain slug and client_id")
    return slug, client_id


def ensure_login(*, interactive: bool, run: Run) -> None:
    """Sign in once and make ``gh`` available for repository operations."""
    if _run(run, ("gh", "auth", "status", "--hostname", "github.com")).returncode == 0:
        return
    if not interactive:
        raise ConnectError("not signed in to GitHub; run " + " ".join(LOGIN))
    print("Sign in to GitHub (browser or pasted token) so this script can start CI")
    if _run(run, LOGIN, capture=False).returncode != 0:
        raise ConnectError("GitHub sign-in did not complete")


def owner_id(owner: str, *, run: Run) -> int:
    """Look up the account selected by ``repos.txt``."""
    result = _run(run, ("gh", "api", f"users/{owner}"))
    try:
        body = json.loads(result.stdout)
        return int(body["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ConnectError(f"cannot look up github.com/{owner}") from exc


def connect(
    *,
    repos_file: Path,
    app_file: Path,
    dry_run: bool,
    interactive: bool,
    run: Run = subprocess.run,
    open_browser: Callable[[str], Any] = webbrowser.open,
    confirm: Callable[[str], str] = input,
) -> int:
    """Install the public app and start one workflow as the verification."""
    try:
        owner, names = read_family(repos_file)
        slug, _ = read_app(app_file)
        ensure_login(interactive=interactive, run=run)
        target = owner_id(owner, run=run)
        url = f"https://github.com/apps/{slug}/installations/new/permissions?target_id={target}"
        print(f"Install {slug} on these {owner} repositories:")
        for name in names:
            print(f"  {name}")
        print("Choose 'Only select repositories'; the app needs no access outside this family.")
        if dry_run:
            print(f"dry-run: would open {url}")
            print("dry-run: no token, private key, or Actions secret would be created")
            print("dry-run: nothing was changed")
            return 0
        open_browser(url)
        if not interactive:
            print("Finish the installation in the browser, then start CI from GitHub.")
            return 0
        confirm("Press Enter after GitHub says the app is installed...")
        repository = f"{owner}/{names[1] if len(names) > 1 else META_NAME}"
        result = _run(run, ("gh", "workflow", "run", "CI", "--repo", repository, "--ref", "main"))
        if result.returncode != 0:
            raise ConnectError(f"the app is installed, but CI could not start on {repository}")
        print(f"Installed. CI started on {repository}; watch it with:")
        print(f"  gh run watch --repo {repository}")
        return 0
    except ConnectError as exc:
        print(f"connect-github: {exc}", file=sys.stderr)
        return 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dry-run", action="store_true", help="say what would happen")
    result.add_argument(
        "--repos-file", type=Path, default=META_ROOT / "repos.txt", help="family manifest"
    )
    result.add_argument("--app-file", type=Path, default=APP_FILE, help="public app metadata")
    return result


def main(argv: Sequence[str] | None = None, *, run: Run = subprocess.run) -> int:
    args = parser().parse_args(argv)
    return connect(
        repos_file=args.repos_file.resolve(),
        app_file=args.app_file.resolve(),
        dry_run=args.dry_run,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        run=run,
    )


if __name__ == "__main__":
    sys.exit(main())
