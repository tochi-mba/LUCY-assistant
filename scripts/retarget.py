#!/usr/bin/env python3
"""Point a copy of the LUCY family at a GitHub owner, without changing uv.lock.

Usage: python scripts/retarget.py OWNER [--root DIR] [--keep-sources] [--dry-run]

Standard library only. All inputs are checked before writing; edits preserve existing
line endings and comments. By default, callers keep using the trusted upstream workflow
that the shared app's broker accepts. ``--self-host-ci`` retargets that workflow too,
for an owner operating a separate app and broker. Run the printed uv lock commands after
creating the copies.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

META_ROOT = Path(__file__).resolve().parents[1]
_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_URL = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)(/?)")
_MANIFEST = re.compile(
    r"^(\s*[A-Za-z0-9_.-]+\s+)(https://github\.com/[^\s#]+)", re.MULTILINE
)
_CALLER = re.compile(
    r"^(\s*uses:\s*['\"]?)[A-Za-z0-9-]+"
    r"(/LUCY-assistant/\.github/workflows/service\.yml@)([^\s'\"#]+)",
    re.MULTILINE,
)
_META_CHECKOUT = re.compile(
    r"^(\s*repository:\s*['\"]?)[A-Za-z0-9-]+(/LUCY-assistant)(?=['\"\s#]|$)",
    re.MULTILINE,
)
_TABLE = re.compile(r"^\s*\[([^\]\r\n]+)\]\s*$")
_SOURCE_GIT = re.compile(r"(\bgit\s*=\s*)(['\"])(https://github\.com/[^'\"\s]+)\2")


@dataclass(frozen=True)
class Edit:
    path: Path
    original: bytes
    replacement: bytes
    count: int


def owner_arg(value: str) -> str:
    if not _OWNER.fullmatch(value) or "--" in value:
        raise argparse.ArgumentTypeError(
            "OWNER must be a GitHub username or organization name"
        )
    return value


def ref_arg(value: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", value)
        or any(part in value for part in ("..", "//", "/."))
        or value.endswith(("/", ".", ".lock"))
    ):
        raise argparse.ArgumentTypeError(
            "--ref must be a branch, tag, or commit without whitespace"
        )
    return value


def _without_comment(line: str) -> str:
    """Keep hashes inside quoted TOML strings while ignoring actual comments."""
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _retarget_url(url: str, owner: str) -> str:
    match = _URL.fullmatch(url)
    if match is None:
        return url
    return f"https://github.com/{owner}/{match[2]}{match[3]}"


def _manifest(text: str) -> tuple[list[str], set[str]]:
    folders = []
    repositories = set()
    for number, raw in enumerate(text.splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        if (
            len(fields) != 2
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", fields[0])
            or _URL.fullmatch(fields[1]) is None
        ):
            raise ValueError(
                f"repos.txt:{number}: expected <folder> <https://github.com/owner/repo>"
            )
        if fields[0].casefold() in {folder.casefold() for folder in folders}:
            raise ValueError(f"repos.txt:{number}: duplicate folder {fields[0]}")
        folders.append(fields[0])
        repositories.add(
            fields[1].rstrip("/").rsplit("/", 1)[1].removesuffix(".git").casefold()
        )
    return folders, repositories


def _source_urls(text: str, owner: str, repositories: set[str]) -> tuple[str, int]:
    # Validate the document before changing any file in the family.
    tomllib.loads(text)
    active = False
    count = 0
    lines = []
    for line in text.splitlines(keepends=True):
        code = _without_comment(line)
        table = _TABLE.match(code.strip())
        if code.lstrip().startswith("["):
            active = bool(table) and (
                table[1] == "tool.uv.sources" or table[1].startswith("tool.uv.sources.")
            )
        if active:

            def replace(match: re.Match[str]) -> str:
                nonlocal count
                url = match[3]
                repository = (
                    url.rstrip("/").rsplit("/", 1)[1].removesuffix(".git").casefold()
                )
                new = _retarget_url(url, owner) if repository in repositories else url
                count += new != url
                return f"{match[1]}{match[2]}{new}{match[2]}"

            changed = _SOURCE_GIT.sub(replace, code)
            line = changed + line[len(code) :]
        lines.append(line)
    return "".join(lines), count


def _rewrite(
    text: str, pattern: re.Pattern[str], replacement: Callable[[re.Match[str]], str]
) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        new = replacement(match)
        count += new != match[0]
        return new

    return pattern.sub(replace, text), count


def plan(
    root: Path, owner: str, ref: str, keep_sources: bool, self_host_ci: bool = False
) -> tuple[list[Edit], list[str], list[str]]:
    manifest = root / "repos.txt"
    contents = manifest.read_bytes()
    folders, repositories = _manifest(contents.decode("utf-8"))
    edits = []
    missing = []
    relock = []

    def add(
        path: Path,
        transform: Callable[[str], tuple[str, int]],
        *,
        original: bytes | None = None,
    ) -> int:
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"refusing to change a file outside --root: {path}")
        if not path.is_file():
            missing.append(path.relative_to(root).as_posix())
            return 0
        before = path.read_bytes() if original is None else original
        after, count = transform(before.decode("utf-8"))
        edits.append(Edit(path, before, after.encode("utf-8"), count))
        return count

    add(
        manifest,
        lambda text: _rewrite(
            text, _MANIFEST, lambda m: m[1] + _retarget_url(m[2], owner)
        ),
        original=contents,
    )
    if self_host_ci:
        add(
            root / ".github/workflows/service.yml",
            lambda text: _rewrite(text, _META_CHECKOUT, lambda m: m[1] + owner + m[2]),
        )
    for folder in folders:
        checkout = root / folder
        if self_host_ci:
            add(
                checkout / ".github/workflows/ci.yml",
                lambda text: _rewrite(
                    text, _CALLER, lambda m: m[1] + owner + m[2] + ref
                ),
            )
        if not keep_sources and add(
            checkout / "pyproject.toml",
            lambda text: _source_urls(text, owner, repositories),
        ):
            relock.append(folder)
    return edits, missing, relock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner", type=owner_arg, metavar="OWNER")
    parser.add_argument(
        "--ref", type=ref_arg, default="v1", help="reusable-workflow ref (default: v1)"
    )
    parser.add_argument(
        "--root", type=Path, default=META_ROOT, help="family checkout directory"
    )
    parser.add_argument(
        "--keep-sources",
        action="store_true",
        help="keep using the original client sources",
    )
    parser.add_argument(
        "--self-host-ci",
        action="store_true",
        help="also point callers at this owner's workflow (requires a separate app and broker)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report changes without writing anything"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        edits, missing, relock = plan(
            root, args.owner, args.ref, args.keep_sources, args.self_host_ci
        )
        if not args.dry_run:
            for edit in edits:
                if edit.count:
                    edit.path.write_bytes(edit.replacement)
    except (OSError, ValueError) as exc:
        print(f"retarget: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print("retarget: dry-run; no files changed")
    for edit in edits:
        print(f"{edit.path.relative_to(root).as_posix()}: {edit.count} replacement(s)")
    for relative in missing:
        print(f"{relative}: missing; skipped")
    if relock:
        print(
            "\nuv.lock was not changed. After creating the forks, run from the family directory:"
        )
        for folder in relock:
            print(f"  uv lock --directory {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
