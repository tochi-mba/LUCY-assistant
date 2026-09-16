"""Retargeting a family changes configuration without rewriting locks or newlines."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import retarget  # noqa: E402


def write(path: Path, text: str, newline: str = "\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def family(root: Path, newline: str = "\n") -> Path:
    files = {
        "repos.txt": (
            "# https://github.com/original/Example.git stays a comment\n"
            "Alpha https://github.com/original/Alpha.git # retain this\n"
            "  Beta\thttps://github.com/original/Beta\n"
        ),
        ".github/workflows/service.yml": (
            "jobs:\n  parity:\n    steps:\n"
            "      - uses: original/LUCY-assistant/.github/actions/family-token@old\n"
            "        with:\n"
            "          broker-url: https://original.example/v1/token # the shared broker\n"
            "      - uses: actions/checkout@v4\n"
            "        with:\n          repository: original/LUCY-assistant\n"
        ),
        "Alpha/.github/workflows/ci.yml": (
            "# uses: original/LUCY-assistant/.github/workflows/service.yml@old\n"
            "jobs:\n  service:\n"
            "    uses: original/LUCY-assistant/.github/workflows/service.yml@old\n"
            "    secrets: inherit\n"
            "  other:\n    uses: external/workflows/.github/workflows/test.yml@v3\n"
        ),
        "Beta/.github/workflows/ci.yml": (
            "jobs:\n  service:\n"
            "    uses: 'original/LUCY-assistant/.github/workflows/service.yml@old' # keep\n"
        ),
        "Alpha/pyproject.toml": (
            "[project]\nname = 'alpha'\n"
            "[tool.uv.sources]\n"
            "# git = 'https://github.com/original/Alpha'\n"
            'alpha = { git = "https://github.com/original/Alpha", tag = "v1" }\n'
            "beta = { git = 'https://github.com/original/Beta.git', subdirectory = 'client' }\n"
            "other = { git = 'https://github.com/external/Other', tag = 'v2' }\n"
            "[tool.example]\ngit = 'https://github.com/original/Alpha'\n"
        ),
        "Beta/pyproject.toml": (
            "[project]\nname = 'beta'\n"
            "[tool.uv.sources.alpha]\n"
            "git = 'https://github.com/original/Alpha' # retain\n"
            "tag = 'v1'\n"
        ),
        "Alpha/uv.lock": "git = 'https://github.com/original/Alpha?tag=v1#abc'\n",
        "Beta/uv.lock": "an opaque lockfile\n",
    }
    for name, contents in files.items():
        write(root / name, contents, newline)
    return root


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_rewrites_family_configuration_and_reports_relocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    assert retarget.main(["new-owner", "--root", str(root)]) == 0
    after = snapshot(root)
    assert after["repos.txt"] == before["repos.txt"].replace(
        b"github.com/original/Alpha", b"github.com/new-owner/Alpha"
    ).replace(b"github.com/original/Beta", b"github.com/new-owner/Beta")
    assert after[".github/workflows/service.yml"] == before[".github/workflows/service.yml"]
    for name in ("Alpha", "Beta"):
        ci = after[f"{name}/.github/workflows/ci.yml"]
        assert b"uses: original/LUCY-assistant/.github/workflows/service.yml@old" in ci or (
            b"uses: 'original/LUCY-assistant/.github/workflows/service.yml@old'" in ci
        )
        assert after[f"{name}/uv.lock"] == before[f"{name}/uv.lock"]
    project = after["Alpha/pyproject.toml"]
    assert b'git = "https://github.com/new-owner/Alpha"' in project
    assert b"git = 'https://github.com/new-owner/Beta.git'" in project
    assert b"git = 'https://github.com/external/Other'" in project
    assert b"# git = 'https://github.com/original/Alpha'" in project
    assert b"[tool.example]\ngit = 'https://github.com/original/Alpha'" in project
    assert b"git = 'https://github.com/new-owner/Alpha' # retain" in after["Beta/pyproject.toml"]
    assert b"# uses: original/LUCY-assistant/" in after["Alpha/.github/workflows/ci.yml"]
    assert (
        b"external/workflows/.github/workflows/test.yml@v3"
        in after["Alpha/.github/workflows/ci.yml"]
    )
    output = capsys.readouterr().out
    assert "repos.txt: 2" in output
    assert "Alpha/pyproject.toml: 2" in output
    assert "uv lock --directory Alpha" in output
    assert "uv lock --directory Beta" in output


def test_custom_ref_and_keep_sources(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    assert (
        retarget.main(
            [
                "someone",
                "--root",
                str(root),
                "--ref",
                "release/private",
                "--keep-sources",
                "--self-host-ci",
            ]
        )
        == 0
    )
    after = snapshot(root)
    for name in ("Alpha", "Beta"):
        assert after[f"{name}/pyproject.toml"] == before[f"{name}/pyproject.toml"]
        assert b"@release/private" in after[f"{name}/.github/workflows/ci.yml"]
    workflow = after[".github/workflows/service.yml"]
    assert b"repository: someone/LUCY-assistant" in workflow
    assert b"uses: someone/LUCY-assistant/.github/actions/family-token@release/private" in workflow
    # Without --broker-url the copy still calls the shared broker.
    assert b"broker-url: https://original.example/v1/token # the shared broker" in workflow
    assert "uv lock --directory" not in capsys.readouterr().out


def test_self_hosted_broker_url_is_rewritten_only_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = family(tmp_path)
    assert (
        retarget.main(
            [
                "someone",
                "--root",
                str(root),
                "--self-host-ci",
                "--broker-url",
                "https://broker.someone.example/v1/token",
            ]
        )
        == 0
    )
    workflow = snapshot(root)[".github/workflows/service.yml"]
    assert b"broker-url: https://broker.someone.example/v1/token # the shared broker" in workflow
    assert b"family-token@v1" in workflow
    assert ".github/workflows/service.yml: 3" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["someone", "--broker-url", "https://broker.example/v1/token"],
        ["someone", "--self-host-ci", "--broker-url", "http://insecure.example/v1/token"],
        ["someone", "--self-host-ci", "--broker-url", "https://x.example/a b"],
    ],
)
def test_broker_url_needs_self_host_ci_and_https(tmp_path: Path, argv: list[str]) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    with pytest.raises(SystemExit) as exit_:
        retarget.main([*argv, "--root", str(root)])
    assert exit_.value.code == 2
    assert snapshot(root) == before


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_dry_run_is_byte_identical_and_reports_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], newline: str
) -> None:
    root = family(tmp_path, newline)
    before = snapshot(root)
    assert retarget.main(["someone", "--root", str(root), "--dry-run"]) == 0
    assert snapshot(root) == before
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "repos.txt: 2" in output
    assert "uv lock --directory Alpha" in output


def test_preserves_crlf_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = family(tmp_path, "\r\n")
    args = ["someone", "--root", str(root)]
    assert retarget.main(args) == 0
    first = snapshot(root)
    for contents in first.values():
        assert b"\n" not in contents.replace(b"\r\n", b"")
    capsys.readouterr()
    assert retarget.main(args) == 0
    assert snapshot(root) == first
    assert "repos.txt: 0" in capsys.readouterr().out


def test_missing_manifest_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert retarget.main(["someone", "--root", str(tmp_path)]) == 2
    assert "repos.txt" in capsys.readouterr().err


def test_missing_checkouts_are_reported_and_do_not_block_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path / "repos.txt", "Alpha https://github.com/original/Alpha.git\n")
    assert retarget.main(["someone", "--root", str(tmp_path)]) == 0
    assert "github.com/someone/Alpha" in (tmp_path / "repos.txt").read_text()
    assert "missing" in capsys.readouterr().out


@pytest.mark.parametrize(
    "owner",
    ["", "../bad", "bad/name", "-bad", "bad-", "bad name", "bad--name", "x" * 40],
)
def test_invalid_owner_is_usage_error_without_writing(tmp_path: Path, owner: str) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    with pytest.raises(SystemExit, match="2"):
        retarget.main([owner, "--root", str(root)])
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "bad ref",
        "bad@ref",
        "../bad",
        "bad\nref",
        "a//b",
        "a/.b",
        "a.lock",
        "a/",
        "a.",
    ],
)
def test_invalid_ref_is_usage_error_without_writing(tmp_path: Path, ref: str) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    with pytest.raises(SystemExit, match="2"):
        retarget.main(["someone", "--ref", ref, "--root", str(root)])
    assert snapshot(root) == before


@pytest.mark.parametrize(
    "entry",
    [
        "../outside https://github.com/old/Repo",
        "Alpha",
        "Alpha https://example.com/Repo",
    ],
)
def test_malformed_manifest_is_rejected_before_any_edit(tmp_path: Path, entry: str) -> None:
    root = family(tmp_path)
    write(root / "repos.txt", entry + "\n")
    before = snapshot(root)
    assert retarget.main(["someone", "--root", str(root)]) == 2
    assert snapshot(root) == before


def test_invalid_toml_is_rejected_before_any_edit(tmp_path: Path) -> None:
    root = family(tmp_path)
    write(root / "Alpha/pyproject.toml", "[broken TOML")
    before = snapshot(root)
    assert retarget.main(["someone", "--root", str(root)]) == 2
    assert snapshot(root) == before


def test_duplicate_manifest_folder_is_rejected(tmp_path: Path) -> None:
    write(
        tmp_path / "repos.txt",
        "Alpha https://github.com/old/Alpha\nalpha https://github.com/old/Beta\n",
    )
    before = snapshot(tmp_path)
    assert retarget.main(["someone", "--root", str(tmp_path)]) == 2
    assert snapshot(tmp_path) == before


def test_source_parser_respects_escaped_quotes_hashes_and_table_boundaries(
    tmp_path: Path,
) -> None:
    root = family(tmp_path)
    text = (
        "[tool.uv.sources]\n"
        'alpha = { git = "https://github.com/original/Alpha/", tag = "escaped\\"#quote" } # keep\n'
        "beta = { git = 'https://github.com/invalid_owner/Beta', tag = 'v1' }\n"
        "[[other]]\ngit = 'https://github.com/original/Alpha'\n"
    )
    write(root / "Alpha/pyproject.toml", text)
    assert retarget.main(["someone", "--root", str(root)]) == 0
    assert (root / "Alpha/pyproject.toml").read_text() == text.replace(
        "github.com/original/Alpha/", "github.com/someone/Alpha/"
    )


def test_outside_root_is_rejected_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    resolve = Path.resolve

    def redirected(path: Path, *args: object, **kwargs: object) -> Path:
        if path == root / "Alpha/pyproject.toml":
            return root.parent / "outside/pyproject.toml"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected)
    assert retarget.main(["someone", "--root", str(root)]) == 2
    assert snapshot(root) == before


@pytest.mark.parametrize("method", ["read_bytes", "write_bytes"])
def test_io_failures_are_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    method: str,
) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    operation = getattr(Path, method)

    def denied(path: Path, *args: object, **kwargs: object) -> object:
        if path == root / "repos.txt":
            raise PermissionError("access denied for test")
        return operation(path, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, method, denied)
        assert retarget.main(["someone", "--root", str(root)]) == 2
    assert "access denied for test" in capsys.readouterr().err
    assert snapshot(root) == before


def test_cli_entrypoint_and_default_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = family(tmp_path)
    before = snapshot(root)
    monkeypatch.setattr(retarget, "META_ROOT", root)
    assert retarget.main(["someone", "--dry-run"]) == 0
    monkeypatch.setattr(sys, "argv", ["retarget.py", "someone", "--root", str(root), "--dry-run"])
    with pytest.raises(SystemExit, match="0"):
        runpy.run_path(str(ROOT / "scripts/retarget.py"), run_name="__main__")
    assert snapshot(root) == before


def test_mixed_newlines_and_no_final_newline_are_preserved(tmp_path: Path) -> None:
    root = family(tmp_path)
    manifest = b"# header\r\nAlpha https://github.com/original/Alpha\nBeta https://github.com/original/Beta"
    (root / "repos.txt").write_bytes(manifest)
    assert retarget.main(["someone", "--root", str(root)]) == 0
    assert (root / "repos.txt").read_bytes() == manifest.replace(b"/original/", b"/someone/")


def test_empty_family_manifest_is_a_noop(tmp_path: Path) -> None:
    write(tmp_path / "repos.txt", "# add repositories here\n\n")
    before = snapshot(tmp_path)
    assert retarget.main(["someone", "--root", str(tmp_path)]) == 0
    assert snapshot(tmp_path) == before
