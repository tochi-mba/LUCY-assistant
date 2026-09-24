"""Notes, as a person would name them: facts, episodes, and the blocks that stay pinned.

Memory-api is the store. This client is the projection. A raw memory row carries
supersession links, access counts and an account id; none of those belong in a tool result.
What the model needs is a title, a body, a trust level and an id it can confirm or correct.

Calls go to Memory-api's two-credential ``/v1/internal/memory`` surface. PackHttp attaches
Lucy's service token as Bearer and the minted person token as subject proof; this module
never sees either, and it never names an account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.transport import Sibling, field, given, moment, number, rows, segment, text

if TYPE_CHECKING:
    from datetime import datetime

    from lucy_api.packs.context import Http

SERVICE = "memory"
AUDIENCE = "memory-api"
INTERNAL = "/v1/internal/memory"

DEFAULT_LIMIT = 10
"""How many notes a search returns unless asked otherwise.

The service will give more. Ten facts is a page a model can actually weigh; forty is a
second conversation stuffed into the first."""

CORRECTION_CARRIES = (
    "kind",
    "scope",
    "profile",
    "session_id",
    "source",
    "trust",
    "value",
    "confidence",
    "importance",
    "occurred_at",
    "expires_at",
)
"""What a correction keeps from the memory it replaces: everything but the words.

Memory-api's correct route takes a whole `MemoryInput`, and a field left out is not
"unchanged", it is that field's default -- `scope: account`, `trust: stated`,
`source: person`. The store then refuses any correction whose scope differs from the
original's ("A correction must preserve the original memory scope", 409), and every note
the hub writes is profile- or session-scoped, so every correction Lucy ever attempted
failed. Where one did get through, the defaults would have rewritten its provenance: an
untrusted note from a page, corrected, would come back `stated` and enter retrieval --
trust laundered by an edit.

`valid_from` is not carried: the store stamps a correction with its own time, and a
correction dated before the memory it replaces is refused.
"""


@dataclass(frozen=True, slots=True)
class Note:
    """One memory, stripped to what a tool result may carry."""

    id: str
    title: str
    body: str
    kind: str = "fact"
    trust: str = "stated"
    source: str = ""
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class Block:
    """A pinned working-memory section, labelled."""

    label: str
    body: str
    char_limit: int = 0


@dataclass(frozen=True, slots=True)
class TopicCard:
    """One cluster, as the index lists it: a title, a count, never the memories.

    `count` is Memory-api's `memory_count`: members somebody has vouched for, the only ones
    retrieval will ever return. `unconfirmed` is the rest -- members that came from a page
    or a tool and have not been confirmed by the person. They are not in `count`, never
    reach retrieval, and are carried only so the index can say they exist rather than
    quietly drop them.

    `trust` is not something Memory-api sends on a topic. It filters server-side instead: a
    topic whose members are all unvouched never reaches the index (`HAVING memory_count>0`
    in its store). So an absent `trust` is the service having vouched, which is why the
    default is `stated`; the field stays so that a topic arriving with any other value is
    still held back by `Topic`, which resolves anything unrecognised to `untrusted`.
    """

    id: str
    key: str = ""
    title: str = ""
    summary: str = ""
    count: int = 0
    unconfirmed: int = 0
    importance: float = 0.0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    trust: str = "stated"


@dataclass(frozen=True, slots=True)
class Draft:
    """What to store. Kind chooses the scope; the pack never invents one."""

    title: str
    body: str
    kind: str
    profile: str
    session_id: str
    source: str = "conversation"
    trust: str = "stated"


class MemoryClient(Protocol):
    """The notes surface, as the pack calls it."""

    async def search(
        self, query: str = "", *, profile: str = "", limit: int = DEFAULT_LIMIT
    ) -> tuple[Note, ...]: ...

    async def listing(
        self, *, profile: str = "", limit: int = DEFAULT_LIMIT
    ) -> tuple[Note, ...]: ...

    async def blocks(self, *, profile: str = "") -> tuple[Block, ...]: ...

    async def remember(self, draft: Draft) -> Note: ...

    async def confirm(self, memory_id: str, *, profile: str = "") -> Note: ...

    async def correct(
        self, memory_id: str, title: str, body: str, *, profile: str = ""
    ) -> Note: ...

    async def forget(self, memory_id: str, *, profile: str = "") -> Note: ...

    async def topics(self, *, profile: str = "") -> tuple[TopicCard, ...]: ...

    async def topic_memories(self, topic_id: str, *, profile: str = "") -> tuple[Note, ...]: ...


class HttpMemoryClient:
    """Memory-api, over the one HTTP seam a capability is allowed."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def search(
        self, query: str = "", *, profile: str = "", limit: int = DEFAULT_LIMIT
    ) -> tuple[Note, ...]:
        payload = await self._api.send(
            "GET",
            f"{INTERNAL}/search",
            params=given(q=query or None, limit=limit, profile=profile or None),
            profile=profile,
        )
        return tuple(_note(row) for row in rows(payload, "data"))

    async def listing(self, *, profile: str = "", limit: int = DEFAULT_LIMIT) -> tuple[Note, ...]:
        payload = await self._api.send(
            "GET",
            INTERNAL,
            params=given(limit=limit, profile=profile or None),
            profile=profile,
        )
        return tuple(_note(row) for row in rows(payload, "data"))

    async def blocks(self, *, profile: str = "") -> tuple[Block, ...]:
        payload = await self._api.send("GET", f"{INTERNAL}/blocks", profile=profile)
        return tuple(
            Block(
                label=text(row, "label"),
                body=text(row, "body"),
                char_limit=number(row, "char_limit"),
            )
            for row in rows(payload, "data")
        )

    async def remember(self, draft: Draft) -> Note:
        payload = {
            "title": draft.title,
            "body": draft.body,
            "kind": draft.kind,
            "source": draft.source,
            "trust": draft.trust,
            **_scope(kind=draft.kind, profile=draft.profile, session_id=draft.session_id),
        }
        return _note(await self._api.send("POST", INTERNAL, body=payload, profile=draft.profile))

    async def confirm(self, memory_id: str, *, profile: str = "") -> Note:
        return _note(
            await self._api.send(
                "POST",
                f"{INTERNAL}/{segment(memory_id)}/confirm",
                profile=profile,
            )
        )

    async def correct(self, memory_id: str, title: str, body: str, *, profile: str = "") -> Note:
        """Replace a memory's words, keeping everything else it was. See `CORRECTION_CARRIES`.

        Two calls, because the correct route needs the whole memory and the hub does not
        hold it: the original is read first and everything but its words is sent back.
        """
        path = f"{INTERNAL}/{segment(memory_id)}"
        original = await self._api.send("GET", path, profile=profile)
        carried = (
            {key: original[key] for key in CORRECTION_CARRIES if key in original}
            if isinstance(original, dict)
            else {}
        )
        return _note(
            await self._api.send(
                "POST",
                f"{path}/correct",
                body={**carried, "title": title, "body": body},
                profile=profile,
            )
        )

    async def forget(self, memory_id: str, *, profile: str = "") -> Note:
        return _note(
            await self._api.send(
                "POST",
                f"{INTERNAL}/{segment(memory_id)}/forget",
                profile=profile,
            )
        )

    async def topics(self, *, profile: str = "") -> tuple[TopicCard, ...]:
        payload = await self._api.send(
            "GET",
            f"{INTERNAL}/topics",
            params=given(profile=profile or None),
            profile=profile,
        )
        return tuple(_topic(row) for row in rows(payload, "data") if isinstance(row, dict))

    async def topic_memories(self, topic_id: str, *, profile: str = "") -> tuple[Note, ...]:
        payload = await self._api.send(
            "GET",
            f"{INTERNAL}/topics/{segment(topic_id)}",
            profile=profile,
        )
        listed = rows(payload, "data") or rows(payload, "memories")
        return tuple(_note(row) for row in listed)


def _scope(*, kind: str, profile: str, session_id: str) -> dict[str, Any]:
    """Episode lives on this session; a fact lives on the profile unless it is about everyone."""
    if kind == "episode":
        return {"scope": "session", "profile": profile, "session_id": session_id}
    if profile:
        return {"scope": "profile", "profile": profile}
    return {"scope": "account"}


def _topic(row: dict[str, Any]) -> TopicCard:
    """One row of Memory-api's topic index, read by the names it actually serialises.

    The names are the service's (`Memory-api/src/memory_api/domain/models.py`, `Topic`), and
    they matter more than they look: the helpers here return a default for a key that is not
    there, so a wrong name is not an error, it is a zero. This parser read `count` and
    `unread` for as long as it existed, and every prompt ever built said "3 topics, 0
    memories" about three topics holding one memory each -- which Lucy then repeated back
    as a reason: "the memory index shows 0 memories."
    """
    return TopicCard(
        id=text(row, "id"),
        key=text(row, "key"),
        title=text(row, "title"),
        summary=text(row, "summary"),
        count=number(row, "memory_count"),
        unconfirmed=number(row, "unconfirmed"),
        importance=_amount(row, "importance"),
        first_seen=moment(row.get("first_seen")),
        last_seen=moment(row.get("last_seen")),
        trust=text(row, "trust", "stated"),
    )


def _amount(row: dict[str, Any], key: str) -> float:
    value = field(row, key)
    try:
        return 0.0 if value is None else float(value)
    except (TypeError, ValueError):
        return 0.0


def _note(row: Any) -> Note:
    if not isinstance(row, dict):
        return Note(id="", title="", body="")
    return Note(
        id=text(row, "id"),
        title=text(row, "title"),
        body=text(row, "body"),
        kind=text(row, "kind", "fact"),
        trust=text(row, "trust", "stated"),
        source=text(row, "source"),
        confirmed=row.get("confirmed_at") not in (None, "", 0),
    )


def as_dict(note: Note) -> dict[str, Any]:
    """The projection a tool result is allowed to serialise."""
    return {
        "id": note.id,
        "title": note.title,
        "body": note.body,
        "kind": note.kind,
        "trust": note.trust,
        "source": note.source,
        "confirmed": note.confirmed,
    }


__all__ = [
    "AUDIENCE",
    "INTERNAL",
    "Block",
    "Draft",
    "HttpMemoryClient",
    "MemoryClient",
    "Note",
    "TopicCard",
    "as_dict",
]
