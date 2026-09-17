"""weftai's result store, made durable, on the one thread SQLite is allowed to run on.

A plan's whole economy is that a tool result never goes back through the model: a search
returns `$hits`, the next step writes `from: $hits`, and the page text is never a token in
anybody's context. That only holds for as long as `$hits` can still be resolved. weftai
ships an in-memory store holding 200 results for 30 minutes, which is the right size for
one chat turn and the wrong size for a conversation somebody reopens on Thursday -- and
the moment the process restarts, every reference in the transcript is a dangling pointer.
So the hub keeps results in the `results` table instead, and `resolve_stored_ref` lets a
client read `$hits[2]` back out of it without asking the model to fetch the page again.

## It is synchronous, and that is the shape of this whole file

`weftai.results.types.ResultStore` is a **sync** Protocol -- five plain methods, nothing
awaited anywhere in it -- while every byte of SQLite in this service belongs to the single
thread inside `SqlWorker`. Those two facts decide everything here. This store is
constructed **on** that thread, holds the connection that thread opened, and its methods
are ordinary sync methods which the worker invokes on our behalf:

    store = await worker.call(SqlResultStore)
    hits = await worker.call(lambda _: store.get(session_id, "hits"))

Nothing in this module may be awaited, and nothing in it may be called from the event
loop. A `sqlite3.Connection` refuses to be used from a thread other than the one that
opened it, so that mistake surfaces as a loud exception rather than as a database two
threads have been interleaving writes into, which is the only reason it is safe to state
the rule in prose here and then rely on it in every method below.

## What survives the round trip

`StoredResult.data` is arbitrary Python and a column is text, so this module owns the
encode and the decode. JSON alone would quietly flatten distinctions weftai depends on --
it has one sequence type and one kind of object key -- so values JSON cannot tell apart
are written as tagged nodes and read back as what they were:

    scalars, lists, str-keyed dicts   plain JSON, unchanged
    tuples                            tagged, because weftai freezes `items` into a tuple
    dicts with non-string keys        tagged pairs; a JSON object key is always a string
    pydantic models                   tagged with an import path, rebuilt by `model_validate`

A dict that already carries the marker key is written as tagged pairs too, and that escape
is load-bearing rather than tidy: most of what is stored here is somebody else's JSON, off
a web page, and a forged node would otherwise be a stranger choosing which class this
process imports.

Three things are honestly lossy, and no amount of tagging fixes them. A model whose class
has since been renamed or removed comes back as the plain dict it was dumped from, because
refusing to read the row at all would be a worse answer than a readable one. A value this
codec has no rule for -- a `Path`, or a `datetime` handed over bare rather than inside a
model -- is stored as its `repr` and read back as that string, so storing a result never
fails the step that produced it. And an enum member subclassing `str` or `int` returns as
the plain string or number underneath it. `data` and `items` also come back as two equal
tuples rather than the one object weftai stored twice; nothing reads them by identity.

## TTL, cap and the clock

Both limits come from the deployment. weftai's defaults are not wrong, they are aimed at a
different lifetime, and a week-long session inheriting a thirty-minute TTL would lose its
references somewhere in the middle of the work they describe. Expiry is computed from
`stored_at` at read time rather than written down as a deadline, so raising the TTL in a
config change revives results that had aged out instead of leaving a generation of rows
stamped with the old policy.

The store stamps `storedAt` from its own clock and ignores the one on the incoming result,
exactly as weftai's own store does: eviction order is only trustworthy if a single clock
decides it. Overflow evicts oldest-first and the ids come back in `SetResult`, where the
runtime turns them into the notice telling the model that a reference it may still be
holding has gone.

## One write is several statements

`set` sweeps, may evict, then inserts, on a connection in autocommit mode. That is three
statements rather than one, and this module deliberately does not open a transaction
around them: the caller already holds the connection, and a caller who needs the result
committed together with the transcript item mentioning it can wrap the call in one. A
store that began its own transaction would make that composition impossible.
"""

from __future__ import annotations

import importlib
import json
import time
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import BaseModel
from weftai.errors import RefResolutionError
from weftai.refs.syntax import parse_ref
from weftai.results.resolve import resolve_ref

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from weftai.results.types import (
        ResultKind,
        ResultStore,
        SetResult,
        StoredResult,
        StoreLimits,
    )
    from weftai.schema.types import Collection


MARKER: Final = "$lucy"
"""The key that makes a JSON object a tagged node. A dict carrying it is escaped on write."""

MILLISECONDS: Final = 1000.0
"""weftai counts in milliseconds; every timestamp column in this schema is in seconds."""

DEFAULT_LIMITS: StoreLimits = {"ttlMs": 7 * 24 * 60 * 60 * MILLISECONDS, "maxResults": 500}
"""A week, and five hundred results per session.

The week is how long somebody plausibly leaves a conversation and comes back to it still
meaning the same thing. Five hundred is generous rather than principled: a result costs a
row and a bounded blob, and the cap is there to stop one runaway session growing without
limit, not to ration something scarce.
"""

_REPLACE: Final = (
    "INSERT OR REPLACE INTO results "
    "(session_id, id, operation, kind, type, count, data_json, items_json, notices_json, "
    "stored_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
)
"""REPLACE rather than an upsert, because a replaced result is the newest in its session.

REPLACE drops the old row and inserts a new one, so the row takes a new `rowid` and sorts
last even when the clock is too coarse to tell the two writes apart. An `ON CONFLICT DO
UPDATE` would keep the original `rowid` and leave a re-run step sitting where it first
landed, which is not where weftai's own store puts it.
"""


def _class_path(kind: type[object]) -> str:
    """Where a class can be found again. The colon separates the module from the qualname."""
    return f"{kind.__module__}:{kind.__qualname__}"


def _encode(value: object) -> Any:
    """One value, as something `json.dumps` accepts and `_decode` can undo."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, BaseModel):
        # `mode="json"` hands pydantic its own round trip: it knows how each field narrows
        # back on `model_validate`, where this codec would only be guessing.
        return {
            MARKER: "model",
            "class": _class_path(type(value)),
            "fields": value.model_dump(mode="json"),
        }
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, tuple):
        return {MARKER: "tuple", "items": [_encode(item) for item in value]}
    if isinstance(value, dict):
        return _encode_mapping(value)
    return {MARKER: "opaque", "type": _class_path(type(value)), "repr": repr(value)}


def _encode_mapping(value: dict[Any, Any]) -> Any:
    """A dict stays a JSON object unless being one would change it."""
    if MARKER not in value and all(isinstance(key, str) for key in value):
        return {key: _encode(item) for key, item in value.items()}
    return {MARKER: "dict", "pairs": [[_encode(key), _encode(item)] for key, item in value.items()]}


def _decode(value: Any) -> Any:
    """The inverse of `_encode`, for everything `_encode` can produce."""
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    node = cast("dict[str, Any]", value)
    tag = node.get(MARKER)
    if tag is None:
        return {key: _decode(item) for key, item in node.items()}
    return _decode_node(str(tag), node)


def _decode_node(tag: str, node: dict[str, Any]) -> Any:
    """One tagged node, back to the shape JSON could not hold on its own."""
    if tag == "tuple":
        return tuple(_decode(item) for item in node["items"])
    if tag == "dict":
        return {_decode(key): _decode(item) for key, item in node["pairs"]}
    if tag == "model":
        return _decode_model(node)
    return node["repr"]


def _decode_model(node: dict[str, Any]) -> Any:
    """A dumped model, rebuilt -- or the dump itself, when its class is no longer here."""
    model = _find_model(str(node["class"]))
    if model is None:
        return node["fields"]
    return model.model_validate(node["fields"])


def _find_model(path: str) -> type[BaseModel] | None:
    """Resolve an import path to a model class, or to nothing at all.

    A durable store outlives the deployment that wrote to it, so a stored class path is a
    claim about last week's code rather than a fact about this process. What it resolves to
    is therefore checked rather than trusted, and a path naming something that is no longer
    a model is treated exactly like a path naming nothing.
    """
    module_name, _, qualname = path.partition(":")
    try:
        found: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            found = getattr(found, part)
    except (ImportError, AttributeError):
        return None
    if isinstance(found, type) and issubclass(found, BaseModel):
        return found
    return None


def _dumps(value: Any) -> str:
    """Compact JSON with key order left alone: sorting a dict would edit the data."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _to_result(row: sqlite3.Row) -> StoredResult:
    items = row["items_json"]
    return {
        "id": row["id"],
        "operation": row["operation"],
        "kind": cast("ResultKind", row["kind"]),
        "type": row["type"],
        "data": _decode(json.loads(row["data_json"])),
        "items": None if items is None else tuple(_decode(json.loads(items))),
        "count": row["count"],
        "notices": tuple(json.loads(row["notices_json"])),
        "storedAt": row["stored_at"] * MILLISECONDS,
    }


class SqlResultStore:
    """weftai's `ResultStore` over the `results` table, scoped by session at every statement.

    Every method carries `session_id` into the WHERE clause, so a result belonging to one
    conversation is not merely hidden from another but unreachable from it: there is no
    lookup here that takes an id on its own.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        limits: StoreLimits | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Bind to the worker thread's connection. `clock` reads seconds, like the column."""
        chosen = DEFAULT_LIMITS if limits is None else limits
        ttl_ms = chosen["ttlMs"]
        cap = chosen["maxResults"]
        if not ttl_ms > 0:
            message = f"A result store TTL must be a positive number of milliseconds, not {ttl_ms}."
            raise ValueError(message)
        if cap < 1:
            message = f"A result store must be allowed to hold at least one result, not {cap}."
            raise ValueError(message)
        self._db = connection
        self._ttl = ttl_ms / MILLISECONDS
        self._cap = cap
        self._clock = time.time if clock is None else clock

    def _cutoff(self) -> float:
        """The `stored_at` at or below which a row has expired. An infinite TTL makes it -inf."""
        return self._clock() - self._ttl

    def get(self, session_id: str, id: str) -> StoredResult | None:  # noqa: A002
        """One live result, or nothing. The argument is named `id` because the Protocol is."""
        row = self._db.execute(
            "SELECT * FROM results WHERE session_id=? AND id=? AND stored_at>?",
            (session_id, id, self._cutoff()),
        ).fetchone()
        return None if row is None else _to_result(row)

    def set(self, session_id: str, result: StoredResult) -> SetResult:
        """Store one result, evicting as many of the oldest as the cap requires."""
        now = self._clock()
        # Expired rows go before anything is counted: a session full of results nobody can
        # see any more must not be the reason a live one is evicted. The cutoff comes from
        # `now` rather than from `_cutoff`, so one instant decides the whole write. Other
        # sessions are swept too, because no janitor runs and an abandoned conversation's
        # results would otherwise sit in the table for as long as the file exists.
        self._db.execute("DELETE FROM results WHERE stored_at<=?", (now - self._ttl,))
        replaced = (
            self._db.execute(
                "SELECT 1 FROM results WHERE session_id=? AND id=?", (session_id, result["id"])
            ).fetchone()
            is not None
        )
        evicted = () if replaced else self._evict(session_id)
        items = result["items"]
        self._db.execute(
            _REPLACE,
            (
                session_id,
                result["id"],
                result["operation"],
                result["kind"],
                result["type"],
                result["count"],
                _dumps(_encode(result["data"])),
                None if items is None else _dumps([_encode(item) for item in items]),
                _dumps(list(result["notices"])),
                now,
            ),
        )
        return {"replaced": replaced, "evicted": evicted, "cap": self._cap if evicted else None}

    def _evict(self, session_id: str) -> tuple[str, ...]:
        """Make room for one more, oldest first. `rowid` breaks the ties a coarse clock leaves."""
        held: int = self._db.execute(
            "SELECT COUNT(*) FROM results WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        surplus = held - self._cap + 1
        if surplus <= 0:
            return ()
        doomed = tuple(
            str(row["id"])
            for row in self._db.execute(
                "SELECT id FROM results WHERE session_id=? ORDER BY stored_at,rowid LIMIT ?",
                (session_id, surplus),
            ).fetchall()
        )
        self._db.executemany(
            "DELETE FROM results WHERE session_id=? AND id=?",
            [(session_id, doomed_id) for doomed_id in doomed],
        )
        return doomed

    def list(self, session_id: str) -> list[StoredResult]:
        """Everything still live in this session, oldest first, as weftai's own store orders it."""
        rows = self._db.execute(
            "SELECT * FROM results WHERE session_id=? AND stored_at>? ORDER BY stored_at,rowid",
            (session_id, self._cutoff()),
        ).fetchall()
        return [_to_result(row) for row in rows]

    def delete(self, session_id: str, id: str) -> bool:  # noqa: A002
        """Whether a live result was removed. An expired one is already gone, so: no."""
        cursor = self._db.execute(
            "DELETE FROM results WHERE session_id=? AND id=? AND stored_at>?",
            (session_id, id, self._cutoff()),
        )
        return cursor.rowcount > 0

    def clear(self, session_id: str) -> None:
        """Forget one session entirely, expired rows included. Clear means clear."""
        self._db.execute("DELETE FROM results WHERE session_id=?", (session_id,))


def resolve_stored_ref(store: ResultStore, session_id: str, ref: str) -> Collection[object] | None:
    """Read `$hits` or `$hits[2]` straight out of the store, with no model in the loop.

    This is the read path behind `GET /v1/sessions/{id}/results/{ref}`, and the reason the
    store is durable at all: somebody asking "what was the third one?" should cost a SELECT
    rather than another search. Parsing and materialising are weftai's own -- `parse_ref`
    and `resolve_ref` -- so a reference means here exactly what it meant in the plan.

    `None` says nothing in this session is stored under that id: it expired, it was
    evicted, or it belongs to a conversation that is not this one. A reference that is
    itself the problem -- malformed, out of range, or naming a step that returned a value
    rather than entities -- raises `RefResolutionError`, whose message already names the
    fix and is written to be shown to whoever asked.
    """
    parsed = parse_ref(ref)
    if not parsed["ok"]:
        raise RefResolutionError(ref, parsed["message"])
    stored = store.get(session_id, parsed["ref"]["id"])
    if stored is None:
        return None
    return resolve_ref(stored, parsed["ref"])


if TYPE_CHECKING:
    # mypy is the only thing that checks this class against weftai's Protocol, and it will
    # only do it where the two actually meet. This is that place; without it, a renamed
    # argument would be found by a failing tool call rather than by the type checker.
    def _satisfies_weftais_protocol(store: SqlResultStore) -> ResultStore:
        return store


__all__ = ["DEFAULT_LIMITS", "MARKER", "SqlResultStore", "resolve_stored_ref"]
