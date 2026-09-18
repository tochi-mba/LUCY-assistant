"""How a model reads and edits a file without using line numbers as a handle.

Line numbers shift the moment anything above them changes, and models are bad at them.
The stable handle is a digest of the file the model last saw, plus an `old_string` located
by a ladder that gets more forgiving only after an exact match has failed. Ambiguity is
refused with the line numbers of every hit; a miss shows the nearest window as a diff.

Nothing here writes. The pack applies the replacement and then asks the sandbox to persist
it, so a validation failure never becomes a corrupted file.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

DIGEST_CHARS = 16
DEFAULT_LINE_LIMIT = 100
MAX_LINE_LIMIT = 2_000
MAX_LINE_CHARS = 2_000
SIMILARITY_FLOOR = 0.6
NEAR_FLOOR = 0.3
EXACT = "exact"
WHITESPACE = "whitespace"
FUZZY = "fuzzy"
BINARY_NOTICE = "this file is binary; it cannot be read or edited as text"
EMPTY_NEEDLE = "old_string is empty; an edit needs a unique snippet to replace"
STALE_NOTICE = (
    "the file changed since you read it; re-read the file and reapply the edit "
    "against the current contents"
)


@dataclass(frozen=True, slots=True)
class Match:
    """One located slice of the original file."""

    start: int
    end: int
    text: str
    rung: str


@dataclass(frozen=True, slots=True)
class Located:
    """Zero, one, or many matches, plus a near-miss diff when there were none."""

    matches: tuple[Match, ...] = ()
    rung: str = ""
    nearest: str = ""

    @property
    def unique(self) -> Match | None:
        return self.matches[0] if len(self.matches) == 1 else None

    def lines_of(self, haystack: str) -> tuple[int, ...]:
        return tuple(_line_at(haystack, match.start) for match in self.matches)


@dataclass(frozen=True, slots=True)
class Applied:
    """A replacement that either happened or explained why it did not."""

    text: str = ""
    match: Match | None = None
    located: Located = Located()
    notice: str = ""

    @property
    def replaced(self) -> bool:
        return self.match is not None


@dataclass(frozen=True, slots=True)
class Window:
    """A numbered slice of a file, with both digests and an honest count."""

    text: str
    numbered: str
    start_line: int
    end_line: int
    total_lines: int
    file_digest: str
    window_digest: str
    truncated: bool
    notice: str


def digest(text: str) -> str:
    """A short sha256 of the exact bytes a later edit must still see."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_CHARS]


def numbered_window(
    text: str,
    *,
    start_line: int = 1,
    limit: int = DEFAULT_LINE_LIMIT,
    truncated: bool = False,
) -> Window:
    """`cat -n` for one window: 1-based numbers, a tab, and `showing N of M`."""
    lines = text.splitlines()
    total = len(lines)
    start = max(1, start_line)
    cap = max(1, min(limit, MAX_LINE_LIMIT))
    if start > total:
        shown: list[str] = []
        end = total
        count = f"showing 0 of {total} lines (start_line {start} is past the end)"
    else:
        end = min(total, start + cap - 1)
        shown = lines[start - 1 : end]
        count = f"showing lines {start}-{end} of {total}" if total else "showing 0 of 0 lines"
    numbered = "\n".join(
        f"{number}\t{_clip(line)}" for number, line in enumerate(shown, start=start)
    )
    window_text = "\n".join(shown)
    file_digest = digest(text)
    prefix = "file fingerprint is of the retrieved prefix, not the whole file; "
    if truncated:
        count = prefix + count
    return Window(
        text=window_text,
        numbered=numbered,
        start_line=start,
        end_line=end,
        total_lines=total,
        file_digest=file_digest,
        window_digest=digest(window_text),
        truncated=truncated,
        notice=count,
    )


def locate(haystack: str, needle: str) -> Located:
    """Walk the application ladder. Stop at the first unique hit, or at ambiguity."""
    if not needle:
        return Located()
    exact = _occurrences(haystack, needle, EXACT)
    if exact:
        return Located(exact, rung=EXACT)
    folded = _folded_windows(haystack, needle, _fold_indent, WHITESPACE)
    if folded:
        return Located(folded, rung=WHITESPACE)
    fuzzy = _fuzzy_windows(haystack, needle)
    if fuzzy:
        return Located(fuzzy, rung=FUZZY)
    return Located(nearest=_nearest_diff(haystack, needle))


def apply_edit(haystack: str, needle: str, replacement: str) -> Applied:
    """Replace the unique match, or return a notice the model can act on."""
    if not needle:
        return Applied(notice=EMPTY_NEEDLE)
    located = locate(haystack, needle)
    match = located.unique
    if match is not None:
        return Applied(
            text=haystack[: match.start] + replacement + haystack[match.end :],
            match=match,
            located=located,
        )
    if located.matches:
        lines = ", ".join(str(line) for line in located.lines_of(haystack))
        notice = (
            "No replacement was performed. Multiple occurrences of old_str in "
            f"lines: {lines}. Please ensure it is unique."
        )
        return Applied(located=located, notice=notice)
    nearest = f"\n{located.nearest}" if located.nearest else ""
    notice = f"No replacement was performed. old_str was not found.{nearest}"
    return Applied(located=located, notice=notice)


def stale_if_changed(current: str, expected: str) -> str:
    """Empty when the digest still matches, otherwise the re-read instruction."""
    if not expected or digest(current) == expected:
        return ""
    return STALE_NOTICE


def validate_text(path: str, content: str) -> str:
    """Parse JSON, TOML or Python before a write. Other suffixes are left alone."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".json":
        return _json_error(content)
    if suffix == ".toml":
        return _toml_error(content)
    if suffix == ".py":
        return _python_error(content)
    return ""


def is_binary(content: str) -> bool:
    return "\0" in content


def _json_error(content: str) -> str:
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        return f"the result is not valid JSON ({exc.msg} at line {exc.lineno})"
    return ""


def _toml_error(content: str) -> str:
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        return f"the result is not valid TOML ({type(exc).__name__})"
    return ""


def _python_error(content: str) -> str:
    try:
        ast.parse(content)
    except SyntaxError as exc:
        line = exc.lineno or 0
        return f"the result is not valid Python (syntax error at line {line})"
    return ""


def _clip(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    omitted = len(line) - MAX_LINE_CHARS
    return f"{line[:MAX_LINE_CHARS]}… [{omitted} characters omitted]"


def _occurrences(haystack: str, needle: str, rung: str) -> tuple[Match, ...]:
    matches: list[Match] = []
    start = 0
    while True:
        found = haystack.find(needle, start)
        if found < 0:
            return tuple(matches)
        end = found + len(needle)
        matches.append(Match(start=found, end=end, text=needle, rung=rung))
        start = found + 1


def _folded_windows(
    haystack: str, needle: str, fold: Callable[[str], str], rung: str
) -> tuple[Match, ...]:
    needle_lines = needle.splitlines()
    if not needle_lines:
        return ()
    folded_needle = tuple(fold(line) for line in needle_lines)
    spans = _line_spans(haystack)
    width = len(needle_lines)
    matches: list[Match] = []
    for index in range(len(spans) - width + 1):
        window = spans[index : index + width]
        if tuple(fold(line) for _start, _end, line in window) != folded_needle:
            continue
        start, end = window[0][0], window[-1][1]
        matches.append(Match(start=start, end=end, text=haystack[start:end], rung=rung))
    return tuple(matches)


def _fold_indent(line: str) -> str:
    return " ".join(line.split())


def _fuzzy_windows(haystack: str, needle: str) -> tuple[Match, ...]:
    anchors = [line for line in needle.splitlines() if line.strip()]
    if not anchors:
        return ()
    first, last = _fold_indent(anchors[0]), _fold_indent(anchors[-1])
    spans = _line_spans(haystack)
    matches: list[Match] = []
    for begin, (start, _end, line) in enumerate(spans):
        if _fold_indent(line) != first:
            continue
        for finish in range(begin, len(spans)):
            if _fold_indent(spans[finish][2]) != last:
                continue
            window_start, window_end = start, spans[finish][1]
            window = haystack[window_start:window_end]
            if difflib.SequenceMatcher(None, needle, window).ratio() < SIMILARITY_FLOOR:
                continue
            matches.append(Match(start=window_start, end=window_end, text=window, rung=FUZZY))
            break
    return tuple(matches)


def _nearest_diff(haystack: str, needle: str) -> str:
    needle_lines = needle.splitlines() or [needle]
    width = max(1, len(needle_lines))
    hay_lines = haystack.splitlines()
    best_ratio = 0.0
    best: list[str] = []
    if not hay_lines:
        return ""
    last = max(1, len(hay_lines) - width + 1)
    for index in range(last):
        window = hay_lines[index : index + width]
        ratio = difflib.SequenceMatcher(None, needle, "\n".join(window)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = window
    if best_ratio < NEAR_FLOOR or not best:
        return ""
    diff = difflib.unified_diff(needle_lines, best, fromfile="old_str", tofile="file", lineterm="")
    return "\n".join(diff)


def _line_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 0
    for line in text.splitlines(keepends=True):
        end = start + len(line)
        spans.append((start, end, line.rstrip("\r\n")))
        start = end
    return spans


def _line_at(text: str, index: int) -> int:
    return text[:index].count("\n") + 1


__all__ = [
    "DEFAULT_LINE_LIMIT",
    "DIGEST_CHARS",
    "MAX_LINE_LIMIT",
    "Applied",
    "Located",
    "Match",
    "Window",
    "apply_edit",
    "digest",
    "is_binary",
    "locate",
    "numbered_window",
    "stale_if_changed",
    "validate_text",
]
