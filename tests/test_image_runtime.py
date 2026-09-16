"""Opt-in regression check against the production Web-search Compose image."""

from __future__ import annotations

import os
import subprocess
import time
import uuid

import pytest


def command(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, check=False
    )


def test_web_search_starts_offline_without_synchronizing_dependencies() -> None:
    if os.environ.get("LUCY_TEST_FAMILY_IMAGES") != "1":
        pytest.skip("set LUCY_TEST_FAMILY_IMAGES=1 after make images")
    name = "lucy-offline-test-" + uuid.uuid4().hex
    try:
        started = command(
            "run",
            "--detach",
            "--name",
            name,
            "--network",
            "none",
            "--env",
            "UV_OFFLINE=1",
            "lucy-family-web-search",
        )
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            state = command("inspect", "--format", "{{.State.Running}}", name)
            if state.stdout.strip() != "true":
                pytest.fail(command("logs", name).stderr)
            probe = command(
                "exec",
                name,
                "python",
                "-c",
                "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8006/healthy', timeout=2).status == 200",
            )
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("Web-search did not become healthy with networking disabled")
    finally:
        command("rm", "--force", name)
