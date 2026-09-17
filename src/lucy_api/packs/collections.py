"""What a list of things is, declared once, so the runtime can do the rest.

An operation that returns `value(object_schema({}))` hands weftai an opaque blob. It has no
idea the thing is a list, it cannot label the entries, it cannot count them, it cannot let a
later step say `$hits[0,2]`, and it cannot trim the rendering to a budget. Everything the
library is for is switched off by that one choice, and the cost shows up as tokens.

A **collection** turns the same data into something the runtime understands:

* **The model sees labels, not records.** `label` is the one line an entry is rendered as,
  which is usually all a model needs to decide which of forty things it wants. The fields
  behind it are fetched only when something asks for them.
* **References work by position.** `$notes[0,2]` resolves against the whole result, not
  against the lines that happened to be shown, so a step can act on something the model
  never actually read.
* **Eight operations come free.** `filter`, `count`, `countBy`, `distinct`, `mostCommon`,
  `first`, `pick` and `details` are generated for every declared collection. They run
  against a stored result rather than re-fetching it, so "how many of those were confirmed"
  costs no network call and no second page of tokens.
* **The formatter can be honest.** Knowing the entry count is what makes `showing 5 of 41`
  possible; an opaque blob can only be truncated silently.

## Why the declarations live together

One module, not one per pack, because `fields` is reachable from the formatter and from a
label -- and anything reachable from a label is reachable by whatever wrote the data. Having
every declaration in one file makes "does any label expose something it should not" a
question somebody can answer by reading one screen, rather than by auditing nine packs.

That is also why no `fields` callback here touches the context. It is handed `ctx` because
the signature allows it, and using it would make a label depend on who is asking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai import collection
from weftai.schema.types import FieldSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from weftai.schema.types import CollectionType

MAX_LABEL = 120
"""How long one rendered line may be.

Labels are built from text a service returned, and a service can return anything. A label
that ran to a thousand characters would spend a page of the window on one entry and push the
other forty out of the rendering entirely."""


def _clip(text: str, limit: int = MAX_LABEL) -> str:
    """One line, bounded. Newlines are folded because a label is a line, not a paragraph."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _get(item: Any, name: str, fallback: str = "") -> str:
    """Read a field from a dataclass or a mapping without caring which it is."""
    if isinstance(item, dict):
        return str(item.get(name, fallback))
    return str(getattr(item, name, fallback))


def _fields(*names: str) -> Callable[[Any], Sequence[FieldSpec[Any]]]:
    """The fields a collection may be filtered, grouped and detailed by.

    Without these the free operations refuse outright -- and rightly, with a message saying
    so: filtering by a field nobody declared would silently return everything, which looks
    like an answer and is not one.

    Naming them explicitly is also the access boundary. A field that is not listed cannot be
    filtered on, grouped by, or pulled out by `pick`, so a record carrying an account id or
    an internal handle does not expose it merely by passing through here.

    The callback is handed the context and ignores it. A field list that varied by who was
    asking would mean two people filtering the same result got different answers, and the
    difference would be invisible to both.
    """

    def getter(key: str) -> Callable[[Any], object]:
        # A named closure rather than a lambda with a default argument: the default-argument
        # trick is the usual way to capture a loop variable and it is also the usual way to
        # let a caller overwrite the captured value by passing a second positional argument.
        def read(item: Any) -> object:
            return _get(item, key)

        return read

    def declared(_context: Any) -> Sequence[FieldSpec[Any]]:
        return [FieldSpec(name, getter(name)) for name in names]

    return declared


NOTE: CollectionType[Any, Any] = collection(
    "note",
    dict,
    label=lambda item: _clip(f"{_get(item, 'title')} — {_get(item, 'body')}"),
    key=lambda item: _get(item, "id"),
    description="Something remembered about the person, with where it came from.",
    fields=_fields("title", "body", "kind", "trust", "source", "confirmed"),
)

HIT: CollectionType[Any, Any] = collection(
    "hit",
    dict,
    label=lambda item: _clip(
        f"{_get(item, 'title')} ({_get(item, 'site') or _get(item, 'source')})"
    ),
    key=lambda item: _get(item, "id") or _get(item, "link"),
    description="One result from a search, before anything has been opened.",
    fields=_fields("title", "site", "source", "snippet"),
)

TRACK: CollectionType[Any, Any] = collection(
    "track",
    dict,
    label=lambda item: _clip(f"{_get(item, 'name')} — {_get(item, 'artist')}"),
    key=lambda item: _get(item, "uri") or _get(item, "id"),
    description="A song, as something that can be queued or played.",
    fields=_fields("name", "artist", "album", "duration_ms"),
)

FILE: CollectionType[Any, Any] = collection(
    "file",
    dict,
    label=lambda item: _clip(f"{_get(item, 'path')} ({_get(item, 'bytes', '0')} bytes)"),
    key=lambda item: _get(item, "path"),
    description="A file in this conversation's sandbox.",
    fields=_fields("path", "bytes", "kind"),
)

CAPABILITY: CollectionType[Any, Any] = collection(
    "capability",
    dict,
    label=lambda item: _clip(f"{_get(item, 'title')} — {_get(item, 'state')}"),
    key=lambda item: _get(item, "id"),
    description="Something the person can do, and whether it is usable right now.",
    fields=_fields("id", "title", "state", "detail"),
)

HELPER: CollectionType[Any, Any] = collection(
    "helper",
    dict,
    label=lambda item: _clip(f"{_get(item, 'role')}: {_get(item, 'objective')}"),
    key=lambda item: _get(item, "id"),
    description="A helper that was started, and what it was asked to do.",
    fields=_fields("id", "role", "objective", "status"),
)

TOPIC: CollectionType[Any, Any] = collection(
    "topic",
    dict,
    label=lambda item: _clip(f"{_get(item, 'title')} — {_get(item, 'summary')}"),
    key=lambda item: _get(item, "id"),
    description="A subject the assistant knows something about.",
    fields=_fields("id", "title", "summary", "count", "trust"),
)

ALL: tuple[CollectionType[Any, Any], ...] = (
    NOTE,
    HIT,
    TRACK,
    FILE,
    CAPABILITY,
    HELPER,
    TOPIC,
)
"""Every declared collection, so the registry can generate the free operations for each."""


__all__ = [
    "ALL",
    "CAPABILITY",
    "FILE",
    "HELPER",
    "HIT",
    "MAX_LABEL",
    "NOTE",
    "TOPIC",
    "TRACK",
]
