"""Run with LUCY_TEST_DOCKER=1 to exercise all service secret mounts in BuildKit.

The actual Dockerfile RUN instructions execute with a checking uv stand-in. No GitHub
account or real credential is used. Full service image builds remain integration gates.
"""

from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "lucy-test-sentinel-never-a-real-credential-7391"


def assert_no_credential_in_archive(archive: Path, credential: bytes) -> None:
    """Inspect image metadata and every layer, including gzip-compressed OCI blobs."""
    with tarfile.open(archive) as image:
        for member in image:
            if not member.isfile():
                continue
            stream = image.extractfile(member)
            assert stream is not None
            compressed = stream.read(2) == b"\x1f\x8b"
            stream.seek(0)
            content = gzip.GzipFile(fileobj=stream) if compressed else stream
            with content:
                overlap = b""
                while chunk := content.read(1024 * 1024):
                    data = overlap + chunk
                    assert credential not in data, f"credential leaked in {member.name}"
                    overlap = data[-len(credential) :]


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("leaked", [False, True])
def test_archive_scanner_detects_credentials_across_chunk_boundaries(
    tmp_path: Path, compressed: bool, leaked: bool
) -> None:
    data = b"x" * (1024 * 1024 - 8) + (SENTINEL.encode() if leaked else b"safe")
    if compressed:
        data = gzip.compress(data)
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as image:
        directory = tarfile.TarInfo("layers")
        directory.type = tarfile.DIRTYPE
        image.addfile(directory)
        member = tarfile.TarInfo("layers/blob")
        member.size = len(data)
        image.addfile(member, io.BytesIO(data))
    if leaked:
        with pytest.raises(AssertionError, match="credential leaked"):
            assert_no_credential_in_archive(archive, SENTINEL.encode())
    else:
        assert_no_credential_in_archive(archive, SENTINEL.encode())


def docker(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, env=env, timeout=600, check=False)


def dependency_runs(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # Logical Docker instructions, retaining the exact original secret setup and flags.
    return [
        match[0]
        for match in re.finditer(r"^RUN(?:[^\n]*\\\n)*[^\n]*", text, re.MULTILINE)
        if re.search(r"\buv\s+sync\b", match[0])
    ]


SHIM = """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

assert sys.argv[1] == "sync"
secret = Path("/run/secrets/github_token")
if os.environ["EXPECT_AUTH"] == "1":
    token = secret.read_text()
    assert token
    assert os.environ["GIT_CONFIG_COUNT"] == "1"
    assert os.environ["GIT_CONFIG_KEY_0"] == f"url.https://x-access-token:{token}@github.com/.insteadOf"
    assert os.environ["GIT_CONFIG_VALUE_0"] == "https://github.com/"
    # Let git itself resolve an origin URL using this process configuration.
    subprocess.run(["git", "init", "-q", "/tmp/probe"], check=True)
    subprocess.run(["git", "-C", "/tmp/probe", "remote", "add", "origin", "https://github.com/owner/repo"], check=True)
    remote = subprocess.check_output(["git", "-C", "/tmp/probe", "remote", "get-url", "origin"], text=True)
    assert remote.strip() == f"https://x-access-token:{token}@github.com/owner/repo"
    # The persisted remote is still the original URL, with no credential.
    assert token not in Path("/tmp/probe/.git/config").read_text()
    import shutil
    shutil.rmtree("/tmp/probe")
else:
    assert not secret.exists() or not secret.read_bytes()
    assert not any(key.startswith("GIT_CONFIG_") for key in os.environ)
assert not Path.home().joinpath(".gitconfig").exists()
"""

AFTER_RUN = '''RUN python -c "import os; from pathlib import Path; assert not any(k.startswith('GIT_CONFIG_') for k in os.environ); assert not Path('/run/secrets/github_token').exists(); assert not Path.home().joinpath('.gitconfig').exists()"'''


@pytest.mark.parametrize("mode", ["absent", "empty", "present"])
def test_every_dependency_run_scopes_credentials_to_the_build_step(
    tmp_path: Path, mode: str
) -> None:
    if os.environ.get("LUCY_TEST_DOCKER") != "1":
        pytest.skip("set LUCY_TEST_DOCKER=1 for BuildKit integration tests")
    if not shutil.which("docker"):
        pytest.fail("LUCY_TEST_DOCKER requires Docker")
    repositories = [
        row.split()[0]
        for row in (ROOT / "repos.txt").read_text().splitlines()
        if row.strip() and not row.lstrip().startswith("#")
    ]
    runs = []
    for name in repositories:
        path = ROOT / name / "Dockerfile"
        assert path.is_file(), f"bootstrap {name} before running Docker integration tests"
        instructions = dependency_runs(path)
        assert instructions, name
        runs.extend(instructions)
    (tmp_path / "uv-shim").write_text(SHIM, encoding="utf-8", newline="\n")
    contents = [
        "# syntax=docker/dockerfile:1",
        "FROM python:3.11-slim-bookworm",
        "RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*",
        "COPY --chmod=755 uv-shim /usr/local/bin/uv",
        "ARG EXPECT_AUTH",
        "ARG SECRET_CASE",
    ]
    for run in runs:
        contents.extend([run, AFTER_RUN])
    (tmp_path / "Dockerfile").write_text("\n".join(contents) + "\n", encoding="utf-8", newline="\n")
    tag = "lucy-secret-test:" + uuid.uuid4().hex
    env = os.environ.copy()
    env["LUCY_DUMMY_SECRET"] = SENTINEL if mode == "present" else ""
    args = ["build", "--progress=plain", "--build-arg", f"EXPECT_AUTH={int(mode == 'present')}"]
    args.extend(["--build-arg", f"SECRET_CASE={mode}"])
    if mode != "absent":
        args.extend(["--secret", "id=github_token,env=LUCY_DUMMY_SECRET"])
    try:
        built = docker(*args, "-t", tag, str(tmp_path), env=env)
        output = built.stdout + built.stderr
        assert SENTINEL.encode() not in output, "build printed the sentinel credential"
        assert built.returncode == 0, output.decode(errors="replace")
        archive = tmp_path / "image.tar"
        saved = docker("save", "--output", str(archive), tag)
        assert saved.returncode == 0, saved.stderr.decode(errors="replace")
        # docker save includes config/history and every layer, even deleted files.
        assert_no_credential_in_archive(archive, SENTINEL.encode())
    finally:
        docker("image", "rm", "--force", tag)
