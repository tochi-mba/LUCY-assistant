#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["cryptography>=42"]
# ///
"""Connect the family's CI to GitHub from the browser: one command, two clicks.

Usage::

    uv run scripts/connect_github.py            # opens the browser: Create, then Install
    uv run scripts/connect_github.py --dry-run  # says what would happen; touches nothing

CI cannot open a browser, so it needs a credential of its own. This script creates a
**GitHub App** for the family through GitHub's manifest flow (you click *Create GitHub
App*) and installs it on the family repositories (you click *Install*). The app can read
their contents and nothing else; each CI job mints a token from it that lasts one hour.

The app's client id and private key become Actions secrets on every family repository,
set with your own ``gh`` login. The key is never written to disk and never printed.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

META_ROOT = Path(__file__).resolve().parents[1]
META_NAME = "LUCY-assistant"
API = "https://api.github.com"
APP_ID_SECRET = "FAMILY_APP_CLIENT_ID"  # noqa: S105 - the secret's name, not a value
APP_KEY_SECRET = "FAMILY_APP_PRIVATE_KEY"  # noqa: S105 - the secret's name, not a value
RETIRED_SECRET = "FAMILY_GITHUB_TOKEN"  # noqa: S105 - the secret's name, not a value
APP_NAME_LIMIT = 34
LOGIN = ("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https")
Run = Callable[..., subprocess.CompletedProcess[str]]
Fetch = Callable[..., tuple[int, Any]]


class ConnectError(Exception):
    """A user-facing failure; the message is already safe to print."""


# --- the family ---------------------------------------------------------------------


def read_family(repos_file: Path) -> tuple[str, list[str]]:
    """Owner plus repository names, meta first, from ``repos.txt``."""
    try:
        text = repos_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConnectError(f"cannot read {repos_file.name}: {exc.strerror}") from exc
    owner = ""
    names: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or "github.com/" not in parts[1]:
            raise ConnectError(f"{repos_file.name}: {line!r} is not '<folder> <https clone URL>'")
        folder, url = parts[0], parts[1]
        _, _, rest = url.partition("github.com/")
        found_owner, _, repo = rest.strip("/").partition("/")
        repo = repo.removesuffix(".git")
        if not found_owner or repo != folder:
            raise ConnectError(
                f"{repos_file.name}: {url} must be https://github.com/<owner>/{folder}.git"
            )
        if owner and found_owner != owner:
            raise ConnectError(f"{repos_file.name}: mixed owners {owner!r} and {found_owner!r}")
        owner = found_owner
        names.append(folder)
    if not owner:
        raise ConnectError(f"{repos_file.name} lists no repositories")
    return owner, [META_NAME, *names]


def app_name(owner: str, requested: str | None) -> str:
    """GitHub App names are unique on GitHub and at most 34 characters."""
    name = (requested or f"{owner} family CI").strip()
    if not name:
        raise ConnectError("--name must not be empty")
    return name[:APP_NAME_LIMIT]


def manifest(owner: str, name: str, redirect_url: str) -> dict[str, Any]:
    """What GitHub creates when the person clicks: read-only on contents, nothing else."""
    return {
        "name": name,
        "url": f"https://github.com/{owner}/{META_NAME}",
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": {"contents": "read", "metadata": "read"},
        "default_events": [],
        "description": (
            "Read-only access to the LUCY family's repositories for their CI. "
            "Created by scripts/connect_github.py in LUCY-assistant."
        ),
    }


# --- processes and HTTP -------------------------------------------------------------


def _run(
    run: Run, args: Sequence[str], *, input: str | None = None, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return run(list(args), input=input, capture_output=capture, text=True, check=False, timeout=120)


def fetch(
    method: str, url: str, *, headers: dict[str, str] | None = None, body: Any = None
) -> tuple[int, Any]:
    """One GitHub API call; the status and the decoded JSON body (``{}`` when empty)."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 - https only
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", f"{META_NAME} connect_github")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https only
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except urllib.error.URLError as exc:
        raise ConnectError(f"{method} {url}: {exc.reason}") from exc
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, {}


def ensure_login(*, interactive: bool, run: Run) -> None:
    """Your own ``gh`` login sets the secrets; the app never does."""
    if _run(run, ("gh", "auth", "status", "--hostname", "github.com")).returncode == 0:
        return
    if not interactive:
        raise ConnectError("not signed in to GitHub; run " + " ".join(LOGIN))
    print("Sign in to GitHub (browser or a pasted token) so this script can set repository secrets")
    if _run(run, LOGIN, capture=False).returncode != 0:
        raise ConnectError("GitHub sign-in did not complete")
    if _run(run, ("gh", "auth", "status", "--hostname", "github.com")).returncode != 0:
        raise ConnectError("GitHub sign-in did not complete")


def account(owner: str, *, run: Run) -> tuple[int, bool]:
    """The owner's numeric id and whether it is an organisation."""
    result = _run(run, ("gh", "api", f"users/{owner}"))
    try:
        data = json.loads(result.stdout or "")
        return int(data["id"]), data.get("type") == "Organization"
    except (ValueError, KeyError, TypeError) as exc:
        raise ConnectError(f"cannot look up github.com/{owner}: is it spelled right?") from exc


# --- the browser handoff ------------------------------------------------------------


class Handoff(ThreadingHTTPServer):
    """A local page that posts the manifest to GitHub and receives the redirect back."""

    daemon_threads = False  # server_close() then waits for the reply to finish sending

    def __init__(self, create_url: str, build_manifest: Callable[[str], dict[str, Any]]):
        super().__init__(("127.0.0.1", 0), HandoffHandler)
        self.state = secrets.token_urlsafe(24)
        self.create_url = f"{create_url}?state={self.state}"
        self.manifest = build_manifest(f"{self.start_url}callback")
        self.code: str | None = None
        self.done = threading.Event()

    @property
    def start_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/"


class HandoffHandler(BaseHTTPRequestHandler):
    server: Handoff

    def log_message(self, *_: Any) -> None:  # quiet; the terminal carries the instructions
        return

    def _page(self, status: int, title: str, body: str) -> None:
        document = (
            "<!doctype html><meta charset='utf-8'><title>LUCY-assistant</title>"
            "<body style='font: 16px/1.5 system-ui; max-width: 40em; margin: 4em auto'>"
            f"<h1>{html.escape(title)}</h1>{body}</body>"
        )
        payload = document.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/":
            form = (
                f"<form id='f' method='post' action='{html.escape(self.server.create_url)}'>"
                "<input type='hidden' name='manifest' "
                f"value='{html.escape(json.dumps(self.server.manifest))}'>"
                "<button>Continue to GitHub</button></form>"
                "<script>document.getElementById('f').submit()</script>"
            )
            self._page(200, "Taking you to GitHub", form)
            return
        if url.path == "/callback":
            query = urllib.parse.parse_qs(url.query)
            if query.get("state", [""])[0] != self.server.state or not query.get("code"):
                self._page(400, "That link is not from this run", "<p>Run the script again.</p>")
                return
            self.server.code = query["code"][0]
            self._page(200, "App created", "<p>Go back to the terminal for the next step.</p>")
            self.server.done.set()
            return
        self._page(404, "Not found", "")


# --- the app's own credentials -----------------------------------------------------


def app_jwt(client_id: str, pem: str, *, now: float | None = None) -> str:
    """A ten-minute JWT signed with the app's key: how an app proves it is itself."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConnectError("GitHub returned a key that is not RSA")
    issued = int(time.time() if now is None else now)

    def part(value: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
            .decode()
            .rstrip("=")
        )

    signing_input = (
        part({"alg": "RS256", "typ": "JWT"})
        + "."
        + part({"iat": issued - 60, "exp": issued + 540, "iss": client_id})
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def installation_for(
    owner: str, client_id: str, pem: str, *, fetch: Fetch
) -> dict[str, Any] | None:
    status, data = fetch(
        "GET",
        f"{API}/app/installations?per_page=100",
        headers={"Authorization": f"Bearer {app_jwt(client_id, pem)}"},
    )
    if status != 200:
        raise ConnectError(f"GitHub could not list the app's installations (HTTP {status})")
    for installation in data:
        if installation.get("account", {}).get("login", "").casefold() == owner.casefold():
            return installation
    return None


def installed_repositories(
    installation_id: int, client_id: str, pem: str, *, fetch: Fetch
) -> set[str]:
    """Which repositories the installation covers, read with a token the app minted."""
    status, data = fetch(
        "POST",
        f"{API}/app/installations/{installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {app_jwt(client_id, pem)}"},
        body={"permissions": {"metadata": "read"}},
    )
    if status != 201 or not data.get("token"):
        raise ConnectError(f"the app could not mint a token (HTTP {status})")
    headers = {"Authorization": f"Bearer {data['token']}"}
    covered: set[str] = set()
    page = 1
    while True:
        status, data = fetch(
            "GET", f"{API}/installation/repositories?per_page=100&page={page}", headers=headers
        )
        if status != 200:
            raise ConnectError(f"the app could not list its repositories (HTTP {status})")
        covered.update(repo["full_name"].casefold() for repo in data.get("repositories", []))
        if len(covered) >= int(data.get("total_count", 0)) or not data.get("repositories"):
            return covered
        page += 1


# --- secrets ------------------------------------------------------------------------


def install_secret(repo: str, name: str, value: str, *, run: Run) -> None:
    result = _run(run, ("gh", "secret", "set", name, "--repo", repo), input=value)
    if result.returncode != 0:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        message = lines[0] if lines else "secret set failed"
        if value.strip() in message:
            message = "secret set failed"
        raise ConnectError(f"{repo}: {name}: {message}")


def retire_secret(repo: str, name: str, *, run: Run) -> bool:
    """Remove a secret from an earlier setup; absent is fine."""
    return _run(run, ("gh", "secret", "delete", name, "--repo", repo)).returncode == 0


# --- the whole flow -----------------------------------------------------------------


def connect(
    *,
    repos_file: Path,
    name: str | None,
    dry_run: bool,
    interactive: bool,
    run: Run,
    fetch: Fetch = fetch,
    open_browser: Callable[[str], Any] = webbrowser.open,
    wait_seconds: float = 900,
    poll_seconds: float = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    try:
        return _connect(
            repos_file=repos_file,
            name=name,
            dry_run=dry_run,
            interactive=interactive,
            run=run,
            fetch=fetch,
            open_browser=open_browser,
            wait_seconds=wait_seconds,
            poll_seconds=poll_seconds,
            sleep=sleep,
        )
    except ConnectError as exc:
        print(f"connect-github: {exc}", file=sys.stderr)
        return 1


def _wait(
    deadline: float, ready: Callable[[], bool], sleep: Callable[[float], None], poll: float
) -> bool:
    while True:
        if ready():
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(poll)


def _connect(
    *,
    repos_file: Path,
    name: str | None,
    dry_run: bool,
    interactive: bool,
    run: Run,
    fetch: Fetch,
    open_browser: Callable[[str], Any],
    wait_seconds: float,
    poll_seconds: float,
    sleep: Callable[[float], None],
) -> int:
    owner, names = read_family(repos_file)
    repos = [f"{owner}/{repo}" for repo in names]
    title = app_name(owner, name)
    ensure_login(interactive=interactive, run=run)
    owner_id, organisation = account(owner, run=run)
    create_url = (
        f"https://github.com/organizations/{owner}/settings/apps/new"
        if organisation
        else "https://github.com/settings/apps/new"
    )
    if dry_run:
        print(f"dry-run: would create the GitHub App {title!r} (contents: read, metadata: read)")
        print(f"dry-run: would install it on {len(repos)} repositories under {owner}")
        for repo in repos:
            print(f"dry-run: gh secret set {APP_ID_SECRET} / {APP_KEY_SECRET} --repo {repo}")
        print("dry-run: nothing was changed")
        return 0

    server = Handoff(create_url, lambda redirect: manifest(owner, title, redirect))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        print(f"Step 1 of 2: your browser will open GitHub. Click 'Create GitHub App' ({title}).")
        print(f"  If it does not open, visit {server.start_url}")
        open_browser(server.start_url)
        if not server.done.wait(wait_seconds):
            raise ConnectError("no answer from GitHub in time; run the script again")
    finally:
        server.shutdown()
        server.server_close()

    status, created = fetch("POST", f"{API}/app-manifests/{server.code}/conversions")
    if status != 201 or not all(key in created for key in ("client_id", "slug", "pem")):
        raise ConnectError(f"GitHub did not finish creating the app (HTTP {status})")
    client_id, slug, pem = str(created["client_id"]), str(created["slug"]), str(created["pem"])
    print(f"Created the GitHub App '{slug}'.")

    install_url = (
        f"https://github.com/apps/{slug}/installations/new/permissions?target_id={owner_id}"
    )
    print("Step 2 of 2: click 'Install', choosing 'Only select repositories' and the family:")
    for repo in repos:
        print(f"  {repo}")
    print(f"  If the page does not open, visit {install_url}")
    open_browser(install_url)
    deadline = time.monotonic() + wait_seconds
    found: dict[str, Any] = {}

    def installed() -> bool:
        installation = installation_for(owner, client_id, pem, fetch=fetch)
        if installation:
            found.update(installation)
        return bool(installation)

    if not _wait(deadline, installed, sleep, poll_seconds):
        raise ConnectError("the app was not installed in time; run the script again")
    installation_id = int(found["id"])
    settings_url = (
        f"https://github.com/organizations/{owner}/settings/installations/{installation_id}"
        if organisation
        else f"https://github.com/settings/installations/{installation_id}"
    )
    wanted = {repo.casefold() for repo in repos}
    reported: set[str] = set()

    def covered() -> bool:
        missing = wanted - installed_repositories(installation_id, client_id, pem, fetch=fetch)
        if missing and missing != reported:
            reported.clear()
            reported.update(missing)
            print("The app cannot see: " + ", ".join(sorted(missing)))
            print(f"  Add them under 'Repository access' at {settings_url} - waiting.")
        return not missing

    if not _wait(deadline, covered, sleep, poll_seconds):
        raise ConnectError(f"still missing repositories; add them at {settings_url} and rerun")
    print(f"Installed on all {len(repos)} family repositories.")

    for repo in repos:
        install_secret(repo, APP_ID_SECRET, client_id, run=run)
        install_secret(repo, APP_KEY_SECRET, pem, run=run)
        retired = retire_secret(repo, RETIRED_SECRET, run=run)
        print(
            f"{repo}: {APP_ID_SECRET} and {APP_KEY_SECRET} set"
            + (f"; {RETIRED_SECRET} removed" if retired else "")
        )
    first = f"{owner}/{names[1]}" if len(names) > 1 else repos[0]
    triggered = _run(run, ("gh", "workflow", "run", "CI", "--repo", first, "--ref", "main"))
    if triggered.returncode == 0:
        print(f"Triggered CI on {first}; watch it with: gh run watch --repo {first}")
    print("Done. CI now reads the family with a one-hour token from this app.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and install the family's GitHub App from the browser; CI gets its key.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="say what would happen; change nothing"
    )
    parser.add_argument(
        "--name",
        help=f"the app's name on GitHub (default: '<owner> family CI', {APP_NAME_LIMIT} chars max)",
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
    return connect(
        repos_file=repos_file,
        name=args.name,
        dry_run=args.dry_run,
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        run=run,
    )


if __name__ == "__main__":
    sys.exit(main())
