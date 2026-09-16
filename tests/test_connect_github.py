"""connect_github.py drives GitHub's manifest flow with a fake GitHub and a fake browser."""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import connect_github  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
APP_ID = 7
CLIENT_ID = "Iv1.test0123456789ab"
INSTALLATION_ID = 42
FAMILY = ["someone/LUCY-assistant", "someone/Alpha", "someone/Beta"]


def write_manifest(path: Path) -> Path:
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\nBeta https://github.com/someone/Beta.git\n",
        encoding="utf-8",
    )
    return path


def verify_jwt(header: str) -> dict:
    """Check the app JWT's signature with the public key and return its claims."""
    token = header.removeprefix("Bearer ")
    signing_input, _, signature = token.rpartition(".")
    KEY.public_key().verify(
        base64.urlsafe_b64decode(signature + "=="),
        signing_input.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    claims = signing_input.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(claims + "=="))


class FakeGitHub:
    """gh (subprocess) and api.github.com (fetch) in one place; nothing real is called."""

    def __init__(self, *, signed_in: bool = True, installs_after: int = 1, grants_after: int = 1):
        self.signed_in = signed_in
        self.installs_after = installs_after
        self.grants_after = grants_after
        self.commands: list[tuple[tuple[str, ...], str | None]] = []
        self.requests: list[tuple[str, str]] = []
        self.secrets: dict[tuple[str, str], str] = {
            (repo, "FAMILY_GITHUB_TOKEN"): "old-session" for repo in FAMILY
        }
        self.code: str | None = None

    def run(self, args, **kwargs):
        argv = tuple(args)
        self.commands.append((argv, kwargs.get("input")))
        code, out = 0, ""
        if argv[:3] == ("gh", "auth", "status"):
            code = 0 if self.signed_in else 1
        elif argv[:3] == ("gh", "auth", "login"):
            self.signed_in = True
        elif argv[:2] == ("gh", "api") and argv[2] == "users/someone":
            out = json.dumps({"id": 555, "type": "User"})
        elif argv[:3] == ("gh", "secret", "set"):
            self.secrets[(argv[5], argv[3])] = kwargs["input"]
        elif argv[:3] == ("gh", "secret", "delete"):
            code = 0 if self.secrets.pop((argv[5], argv[3]), None) else 1
        elif argv[:3] == ("gh", "workflow", "run"):
            pass
        else:
            raise AssertionError(f"unexpected gh call: {argv}")
        return subprocess.CompletedProcess(args, code, out, "")

    def fetch(self, method, url, *, headers=None, body=None):
        self.requests.append((method, url))
        headers = headers or {}
        if method == "POST" and "/app-manifests/" in url:
            assert url == f"https://api.github.com/app-manifests/{self.code}/conversions"
            return 201, {
                "id": APP_ID,
                "client_id": CLIENT_ID,
                "slug": "someone-family-ci",
                "pem": PEM,
                "html_url": "x",
            }
        if url.startswith("https://api.github.com/app/installations?"):
            assert verify_jwt(headers["Authorization"])["iss"] == CLIENT_ID
            self.installs_after -= 1
            if self.installs_after >= 0:
                return 200, []
            return 200, [
                {"id": 9, "account": {"login": "unrelated"}},
                {"id": INSTALLATION_ID, "account": {"login": "Someone"}},
            ]
        if url == f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens":
            assert verify_jwt(headers["Authorization"])["iss"] == CLIENT_ID
            assert body == {"permissions": {"metadata": "read"}}
            return 201, {"token": "ghs_installation_test"}
        if url.startswith("https://api.github.com/installation/repositories"):
            assert headers["Authorization"] == "Bearer ghs_installation_test"
            self.grants_after -= 1
            names = FAMILY[:2] if self.grants_after >= 0 else FAMILY
            return 200, {
                "total_count": len(names),
                "repositories": [{"full_name": name} for name in names],
            }
        raise AssertionError(f"unexpected request: {method} {url}")

    def browser(self, url: str) -> None:
        """What a person does: submit the manifest, get redirected back with a code."""
        if "installations/new" in url:
            assert url.endswith("?target_id=555")
            return

        def click() -> None:
            with urllib.request.urlopen(url) as page:  # noqa: S310 - 127.0.0.1
                text = page.read().decode()
            action = re.search(r"action='([^']+)'", text)[1]
            manifest = json.loads(
                re.search(r"name='manifest' value='([^']+)'", text)[1]
                .replace("&quot;", '"')
                .replace("&#x27;", "'")
            )
            assert manifest["default_permissions"] == {"contents": "read", "metadata": "read"}
            assert manifest["public"] is False
            assert manifest["redirect_url"] == url + "callback"
            state = action.split("state=")[1]
            self.code = "code-" + state[:6]
            with urllib.request.urlopen(f"{url}callback?code={self.code}&state={state}") as done:  # noqa: S310
                assert done.status == 200

        threading.Thread(target=click, daemon=True).start()


def connect(tmp_path: Path, gh: FakeGitHub, **overrides) -> int:
    options = dict(
        repos_file=write_manifest(tmp_path / "repos.txt"),
        name=None,
        dry_run=False,
        interactive=False,
        run=gh.run,
        fetch=gh.fetch,
        open_browser=gh.browser,
        wait_seconds=10,
        poll_seconds=0,
        sleep=lambda _: None,
    )
    options.update(overrides)
    return connect_github.connect(**options)


def test_two_clicks_create_install_and_hand_ci_the_app(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert connect(tmp_path, gh) == 0
    for repo in FAMILY:
        assert gh.secrets[(repo, "FAMILY_APP_CLIENT_ID")] == CLIENT_ID
        assert gh.secrets[(repo, "FAMILY_APP_PRIVATE_KEY")] == PEM
        assert (repo, "FAMILY_GITHUB_TOKEN") not in gh.secrets
    assert not any(PEM.strip() in part for command, _ in gh.commands for part in command)
    assert ("gh", "workflow", "run", "CI", "--repo", "someone/Alpha", "--ref", "main") in [
        command for command, _ in gh.commands
    ]
    out, err = capsys.readouterr()
    assert PEM.strip() not in out + err
    assert "Click 'Create GitHub App'" in out
    assert "click 'Install'" in out
    assert "Installed on all 3 family repositories" in out
    assert "FAMILY_GITHUB_TOKEN removed" in out


def test_waits_for_missing_repositories_and_names_them_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(installs_after=2, grants_after=3)
    assert connect(tmp_path, gh) == 0
    out = capsys.readouterr().out
    assert out.count("The app cannot see: someone/beta") == 1
    assert "https://github.com/settings/installations/42" in out
    listings = [url for _, url in gh.requests if "installation/repositories" in url]
    assert len(listings) == 4


def test_dry_run_neither_serves_nor_opens_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert connect(tmp_path, gh, dry_run=True) == 0
    assert gh.requests == []
    assert not any(command[:3] == ("gh", "secret", "set") for command, _ in gh.commands)
    out = capsys.readouterr().out
    assert "someone family CI" in out
    assert "nothing was changed" in out


def test_a_stale_or_forged_callback_is_refused(tmp_path: Path) -> None:
    server = connect_github.Handoff(
        "https://github.com/settings/apps/new", lambda r: connect_github.manifest("o", "n", r)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{server.start_url}callback?code=x&state=wrong")  # noqa: S310
        assert refused.value.code == 400
        refused.value.close()  # the error object holds the response socket
        assert server.code is None and not server.done.is_set()
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{server.start_url}elsewhere")  # noqa: S310
        assert missing.value.code == 404
        missing.value.close()
    finally:
        server.shutdown()
        server.server_close()


def test_no_answer_from_github_times_out_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    assert connect(tmp_path, gh, open_browser=lambda url: None, wait_seconds=0.2) == 1
    assert "no answer from GitHub" in capsys.readouterr().err
    assert gh.requests == []


def test_conversion_failure_is_reported_without_secrets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub()
    real = gh.fetch

    def failing(method, url, **kwargs):
        if "/app-manifests/" in url:
            gh.requests.append((method, url))
            return 422, {"message": "Validation Failed"}
        return real(method, url, **kwargs)

    assert connect(tmp_path, gh, fetch=failing) == 1
    assert "HTTP 422" in capsys.readouterr().err
    assert not any(command[:3] == ("gh", "secret", "set") for command, _ in gh.commands)


def test_not_signed_in_without_a_terminal_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(signed_in=False)
    assert connect(tmp_path, gh) == 1
    assert "gh auth login" in capsys.readouterr().err
    assert gh.requests == []


def test_signs_in_first_when_a_terminal_is_available(tmp_path: Path) -> None:
    gh = FakeGitHub(signed_in=False)
    assert connect(tmp_path, gh, interactive=True) == 0
    assert connect_github.LOGIN in [command for command, _ in gh.commands]


def test_app_name_defaults_and_is_capped_at_github_limit() -> None:
    assert connect_github.app_name("someone", None) == "someone family CI"
    assert connect_github.app_name("someone", "  Custom  ") == "Custom"
    assert len(connect_github.app_name("a" * 39, None)) == connect_github.APP_NAME_LIMIT
    with pytest.raises(connect_github.ConnectError):
        connect_github.app_name("someone", "   ")


def test_jwt_is_signed_by_the_app_key_and_short_lived() -> None:
    claims = verify_jwt(connect_github.app_jwt(CLIENT_ID, PEM, now=1_000_000))
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": CLIENT_ID}


def test_organisation_owner_uses_the_organisation_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGitHub(grants_after=2)
    real = gh.run

    def org_run(args, **kwargs):
        if tuple(args)[:3] == ("gh", "api", "users/someone"):
            gh.commands.append((tuple(args), None))
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"id": 1, "type": "Organization"}), ""
            )
        return real(args, **kwargs)

    def browser(url: str) -> None:
        if "installations/new" in url:
            assert url.endswith("?target_id=1")
            return
        gh.browser(url)

    assert connect(tmp_path, gh, run=org_run, open_browser=browser) == 0
    assert "github.com/organizations/someone/settings/installations/42" in capsys.readouterr().out


def test_mixed_owners_are_refused_before_anything_happens(tmp_path: Path) -> None:
    path = tmp_path / "mixed.txt"
    path.write_text(
        "Alpha https://github.com/someone/Alpha.git\nBeta https://github.com/other/Beta.git\n",
        encoding="utf-8",
    )
    gh = FakeGitHub()
    assert connect(tmp_path, gh, repos_file=path) == 1
    assert gh.commands == [] and gh.requests == []


def test_main_wires_flags(tmp_path: Path) -> None:
    gh = FakeGitHub()
    manifest = write_manifest(tmp_path / "repos.txt")
    assert connect_github.main(["--dry-run", "--repos-file", str(manifest)], run=gh.run) == 0
