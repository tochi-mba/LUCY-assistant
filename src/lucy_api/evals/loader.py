"""Reading scenario files, and refusing every one that says something the harness cannot do.

**One scenario per file.** The file's stem is the scenario's name and its folder is the
suite, so neither can disagree with anything: there is no ``name =`` to drift from the
filename a person searches for. Files run in name order.

**Strict, and specific about it.** An unknown key is an error that names the file, the key
and the keys the table does take, with the nearest spelling when there is one -- a typed
``reply_match`` that was silently ignored would be an expectation that never ran, which is
the one failure an eval harness must not have. Types are checked, every regex is compiled
here, and contradictions (an operation that must both run and not run) are refused, so a
scenario that loads is a scenario that means what it says.

Positions are 1-based -- ``turns[2]`` is the second turn -- because that is how the report
numbers them.

The shipped suites are package data, read through :mod:`importlib.resources` so the
command works from an installed wheel exactly as it does from a checkout.
"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_api.evals.scenario import (
    APPROVE_VALUES,
    COMPLETED,
    IGNORE,
    INPUT_REQUIRED,
    OK,
    PERMISSION_MODES,
    STEP_STATUSES,
    TURN_STATUSES,
    YES,
    Expect,
    Invocation,
    OpMatch,
    Pattern,
    ResultExpect,
    Scenario,
    Suite,
    TurnSpec,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from importlib.resources.abc import Traversable

PACKAGE = "lucy_api.evals"
FOLDER = "scenarios"
DEFAULT_SUITE = "default"
SHIPPED = "shipped"
SUFFIX = ".toml"

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
"""A scenario or suite name: what a person types after ``--scenario`` or ``--suite``."""

TAG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]*$")
OPERATION = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*\.[A-Za-z][A-Za-z0-9_.-]*$")
OP_PATTERN = re.compile(r"^[A-Za-z0-9_*?\[\]!-]+\.[A-Za-z0-9_*?\[\]!.-]+$")

SCENARIO_KEYS = ("summary", "tags", "permission_mode", "incognito", "requires", "seed", "turns")
TURN_KEYS = ("say", "approve", "timeout_seconds", "expect", "verify")
EXPECT_KEYS = (
    "status",
    "termination",
    "ran",
    "not_ran",
    "not_attempted",
    "approvals",
    "reply_matches",
    "reply_avoids",
    "reply_nonempty",
    "no_leaks",
    "max_seconds",
    "results",
)
RESULT_KEYS = ("matches", "avoids")
OP_KEY = "op"
INVOCATION_KEYS = (OP_KEY, "input", "status", "output_matches", "output_avoids")

CONTRADICTIONS = (("ran", "not_ran"), ("ran", "not_attempted"), ("approvals", "not_attempted"))
"""Pairs of lists an operation cannot sit in together; each pair is a scenario that
cannot pass whatever the model does."""


class ScenarioError(ValueError):
    """A scenario file that cannot be used, named down to the key that is wrong."""


def shipped_suites() -> tuple[str, ...]:
    """Every suite that ships with the package, by name."""
    folder = files(PACKAGE) / FOLDER
    return tuple(sorted(item.name for item in folder.iterdir() if item.is_dir()))


def load_suite(reference: str) -> Suite:
    """A shipped suite by name, or a folder of files (or one file) by path."""
    if reference in shipped_suites() and not _looks_like_path(reference):
        folder = files(PACKAGE) / FOLDER / reference
        shipped = sorted(
            (item for item in folder.iterdir() if item.name.endswith(SUFFIX)),
            key=lambda item: item.name,
        )
        return _suite(reference, SHIPPED, ((f"{reference}/{item.name}", item) for item in shipped))
    path = Path(reference)
    if path.is_dir():
        found = sorted(path.glob(f"*{SUFFIX}"))
        if not found:
            message = f"{path}: no {SUFFIX} scenario files in this folder"
            raise ScenarioError(message)
        suite = path.resolve().name
        return _suite(suite, str(path), ((str(entry), entry) for entry in found))
    if path.is_file() and path.suffix == SUFFIX:
        suite = path.resolve().parent.name
        return _suite(suite, str(path), ((str(path), path),))
    names = ", ".join(shipped_suites())
    message = (
        f"no suite called {reference!r}: the shipped suites are {names}, and a path must "
        f"be a folder of {SUFFIX} files or one {SUFFIX} file"
    )
    raise ScenarioError(message)


def parse_scenario(raw: bytes, *, name: str, suite: str, path: str) -> Scenario:
    """One file's bytes, validated into a :class:`Scenario`."""
    if not NAME.match(name):
        message = f"{path}: a scenario's file name is its name: letters, digits, `-`, `_`, `.`"
        raise ScenarioError(message)
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        message = f"{path}: not UTF-8 text"
        raise ScenarioError(message) from exc
    except tomllib.TOMLDecodeError as exc:
        message = f"{path}: not valid TOML: {exc}"
        raise ScenarioError(message) from exc
    table = _Table(parsed, file=path, where="", allowed=SCENARIO_KEYS)
    turns = tuple(_turn(entry) for entry in table.tables("turns", allowed=TURN_KEYS, required=True))
    return Scenario(
        name=name,
        suite=suite,
        path=path,
        digest=hashlib.sha256(raw).hexdigest(),
        summary=table.text("summary", required=True),
        turns=turns,
        tags=table.words("tags", TAG, "a tag is lower-case letters, digits and `-`"),
        permission_mode=table.choice("permission_mode", PERMISSION_MODES, "ask"),
        incognito=table.flag("incognito", default=False),
        requires=table.words("requires", CAPABILITY, "a capability id, like `research`"),
        seed=tuple(_invocation(entry) for entry in table.tables("seed", allowed=INVOCATION_KEYS)),
    )


def _suite(name: str, origin: str, entries: Iterable[tuple[str, Traversable]]) -> Suite:
    if not NAME.match(name):
        message = f"{origin}: a suite's folder name is its name: letters, digits, `-`, `_`, `.`"
        raise ScenarioError(message)
    scenarios: list[Scenario] = []
    for label, entry in entries:
        try:
            raw = entry.read_bytes()
        except OSError as exc:
            message = f"{label}: cannot read it: {exc.strerror or exc}"
            raise ScenarioError(message) from exc
        stem = entry.name.removesuffix(SUFFIX)
        scenarios.append(parse_scenario(raw, name=stem, suite=name, path=label))
    return Suite(name=name, origin=origin, scenarios=tuple(scenarios))


def _looks_like_path(reference: str) -> bool:
    return any(mark in reference for mark in ("/", "\\")) or reference.endswith(SUFFIX)


def _turn(table: _Table) -> TurnSpec:
    say = table.text("say", required=True)
    approve = table.choice("approve", APPROVE_VALUES, YES)
    timeout = table.seconds("timeout_seconds")
    expect_table = table.table("expect", allowed=EXPECT_KEYS)
    expect = _expect(expect_table, approve=approve) if expect_table else _defaults(approve)
    verify = tuple(_invocation(entry) for entry in table.tables("verify", allowed=INVOCATION_KEYS))
    return TurnSpec(say=say, approve=approve, timeout_seconds=timeout, expect=expect, verify=verify)


def _defaults(approve: str) -> Expect:
    status = INPUT_REQUIRED if approve == IGNORE else COMPLETED
    return Expect(status=status, reply_nonempty=status == COMPLETED)


def _expect(table: _Table, *, approve: str) -> Expect:
    status = table.choice("status", TURN_STATUSES, _defaults(approve).status)
    lists = {key: table.ops(key) for key in ("ran", "not_ran", "not_attempted", "approvals")}
    for first, second in CONTRADICTIONS:
        clash = {op.source for op in lists[first]} & {op.source for op in lists[second]}
        if clash:
            named = ", ".join(f"`{source}`" for source in sorted(clash))
            raise table.error(first, f"{named} is also in {second}; no turn can pass both")
    results = table.table("results", allowed=None)
    return Expect(
        status=status,
        termination=table.text("termination") or None,
        ran=lists["ran"],
        not_ran=lists["not_ran"],
        not_attempted=lists["not_attempted"],
        approvals=lists["approvals"],
        reply_matches=table.patterns("reply_matches"),
        reply_avoids=table.patterns("reply_avoids"),
        reply_nonempty=table.flag("reply_nonempty", default=status == COMPLETED),
        no_leaks=table.flag("no_leaks", default=True),
        max_seconds=table.seconds("max_seconds"),
        results=_results(results) if results else (),
    )


def _results(table: _Table) -> tuple[ResultExpect, ...]:
    found: list[ResultExpect] = []
    for key in table.names():
        op = table.op_key(key)
        entry = table.entry(key, allowed=RESULT_KEYS)
        found.append(
            ResultExpect(op=op, matches=entry.patterns("matches"), avoids=entry.patterns("avoids"))
        )
    return tuple(found)


def _invocation(table: _Table) -> Invocation:
    op = table.text(OP_KEY, required=True)
    if not OPERATION.match(op):
        message = f"`{op}` is not an operation; write one like `workspace.read`"
        raise table.error(OP_KEY, message)
    return Invocation(
        op=op,
        input=table.mapping("input"),
        status=table.choice("status", STEP_STATUSES, OK),
        output_matches=table.patterns("output_matches"),
        output_avoids=table.patterns("output_avoids"),
    )


class _Table:
    """One TOML table, read with its position so every error can say where it is."""

    def __init__(
        self,
        values: dict[str, Any],
        *,
        file: str,
        where: str,
        allowed: tuple[str, ...] | None,
    ) -> None:
        self._values = values
        self._file = file
        self._where = where
        if allowed is not None:
            for key in values:
                if key not in allowed:
                    raise self.error(key, _unknown(key, allowed))

    def names(self) -> tuple[str, ...]:
        return tuple(self._values)

    def error(self, key: str, message: str) -> ScenarioError:
        where = f"{self._where}.{key}" if self._where else key
        return ScenarioError(f"{self._file}: {where}: {message}")

    def text(self, key: str, *, required: bool = False) -> str:
        value = self._values.get(key)
        if value is None:
            if required:
                raise self.error(key, "is required")
            return ""
        if not isinstance(value, str) or not value.strip():
            raise self.error(key, "must be a non-empty string")
        return value

    def flag(self, key: str, *, default: bool) -> bool:
        value = self._values.get(key, default)
        if not isinstance(value, bool):
            raise self.error(key, "must be true or false")
        return value

    def seconds(self, key: str) -> float | None:
        value = self._values.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise self.error(key, "must be a number of seconds")
        if not math.isfinite(value) or value <= 0:
            raise self.error(key, "must be more than zero seconds")
        return float(value)

    def choice(self, key: str, choices: tuple[str, ...], default: str) -> str:
        value = self._values.get(key, default)
        if value not in choices:
            allowed = ", ".join(f'"{choice}"' for choice in choices)
            raise self.error(key, f"must be one of {allowed}, not {value!r}")
        return str(value)

    def words(self, key: str, shape: re.Pattern[str], hint: str) -> tuple[str, ...]:
        return self._list(key, lambda where, word: _word(self, where, word, shape, hint))

    def patterns(self, key: str) -> tuple[Pattern, ...]:
        return self._list(key, lambda where, source: _pattern(self, where, source))

    def ops(self, key: str) -> tuple[OpMatch, ...]:
        return self._list(key, lambda where, source: _op(self, where, source))

    def op_key(self, key: str) -> OpMatch:
        return _op(self, key, key)

    def mapping(self, key: str) -> dict[str, Any]:
        value = self._values.get(key, {})
        if not isinstance(value, dict):
            raise self.error(key, 'must be a table, like { path = "notes.md" }')
        _json_compatible(self, key, value)
        return value

    def table(self, key: str, *, allowed: tuple[str, ...] | None) -> _Table | None:
        """The table under ``key``, or ``None`` when the file leaves it out."""
        return None if key not in self._values else self.entry(key, allowed=allowed)

    def entry(self, key: str, *, allowed: tuple[str, ...] | None) -> _Table:
        value = self._values[key]
        if not isinstance(value, dict):
            raise self.error(key, "must be a table")
        return _Table(value, file=self._file, where=self._child(key), allowed=allowed)

    def tables(self, key: str, *, allowed: tuple[str, ...], required: bool = False) -> list[_Table]:
        value = self._values.get(key)
        if value is None:
            if required:
                raise self.error(key, f"is required: add at least one [[{key}]] table")
            return []
        if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
            raise self.error(key, f"must be written as [[{key}]] tables")
        if required and not value:
            raise self.error(key, f"is required: add at least one [[{key}]] table")
        return [
            _Table(entry, file=self._file, where=f"{self._child(key)}[{index}]", allowed=allowed)
            for index, entry in enumerate(value, start=1)
        ]

    def _child(self, key: str) -> str:
        return f"{self._where}.{key}" if self._where else key

    def _list[T](self, key: str, convert: Callable[[str, str], T]) -> tuple[T, ...]:
        """A list of strings, each converted with its own position for the error it may raise."""
        value = self._values.get(key, [])
        if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
            raise self.error(key, "must be a list of strings")
        seen: set[str] = set()
        for entry in value:
            if entry in seen:
                raise self.error(key, f"lists {entry!r} twice")
            seen.add(entry)
        return tuple(
            convert(f"{key}[{index}]", entry) for index, entry in enumerate(value, start=1)
        )


def _unknown(key: str, allowed: tuple[str, ...]) -> str:
    near = difflib.get_close_matches(key, allowed, n=1)
    guess = f"; did you mean `{near[0]}`?" if near else "."
    takes = ", ".join(f"`{name}`" for name in allowed)
    return f"unknown key{guess} This table takes {takes}"


def _word(table: _Table, where: str, word: str, shape: re.Pattern[str], hint: str) -> str:
    if not shape.match(word):
        message = f"{word!r} is not valid: {hint}"
        raise table.error(where, message)
    return word


def _pattern(table: _Table, where: str, source: str) -> Pattern:
    try:
        compiled = re.compile(source, re.IGNORECASE)
    except re.error as exc:
        message = f"not a valid regular expression: {exc}"
        raise table.error(where, message) from exc
    return Pattern(source=source, regex=compiled)


def _op(table: _Table, where: str, source: str) -> OpMatch:
    alternatives = tuple(part.strip() for part in source.split("|"))
    for part in alternatives:
        if not OP_PATTERN.match(part):
            message = (
                f"{source!r} is not an operation pattern; write `capability.operation`, "
                "`a|b` for either, or `notes.*` for any"
            )
            raise table.error(where, message)
    return OpMatch(source=source, alternatives=alternatives)


def _json_compatible(table: _Table, where: str, value: object) -> None:
    """Invocation input travels as JSON, which has no dates: say so here, not at the hub."""
    if isinstance(value, dict):
        for key, inner in value.items():
            _json_compatible(table, f"{where}.{key}", inner)
    elif isinstance(value, list):
        for index, inner in enumerate(value, start=1):
            _json_compatible(table, f"{where}[{index}]", inner)
    elif not isinstance(value, str | int | float | bool):
        raise table.error(where, "a TOML date or time has no JSON form; write it as a string")


__all__ = [
    "DEFAULT_SUITE",
    "SHIPPED",
    "ScenarioError",
    "load_suite",
    "parse_scenario",
    "shipped_suites",
]
