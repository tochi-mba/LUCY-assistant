"""Notes, as a person would name them: facts, episodes, and the blocks that stay pinned.

Memory-api is the store. This client is the projection. A raw memory row carries
supersession links, access counts and an account id; none of those belong in a tool result.
What the model needs is a title, a body, a trust level and an id it can confirm or correct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.transport import Sibling, given, number, rows, segment, text

if TYPE_CHECKING:
    from lucy_api.packs.context import Http

SERVICE = "memory"
AUDIENCE = "memory-api"

DEFAULT_LIMIT = 10
"""How many notes a search returns unless asked otherwise.

The service will give more. Ten facts is a page a model can actually weigh; forty is a
second conversation stuffed into the first."""


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


class HttpMemoryClient:
    """Memory-api, over the one HTTP seam a capability is allowed."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def search(
        self, query: str = "", *, profile: str = "", limit: int = DEFAULT_LIMIT
    ) -> tuple[Note, ...]:
        payload = await self._api.send(
            "GET",
            "/v1/memory/search",
            params=given(q=query or None, limit=limit, profile=profile or None),
            profile=profile,
        )
        return tuple(_note(row) for row in rows(payload, "data"))

    async def listing(self, *, profile: str = "", limit: int = DEFAULT_LIMIT) -> tuple[Note, ...]:
        payload = await self._api.send(
            "GET",
            "/v1/memory",
            params=given(limit=limit, profile=profile or None),
            profile=profile,
        )
        return tuple(_note(row) for row in rows(payload, "data"))

    async def blocks(self, *, profile: str = "") -> tuple[Block, ...]:
        payload = await self._api.send("GET", "/v1/memory/blocks", profile=profile)
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
        return _note(
            await self._api.send("POST", "/v1/memory", body=payload, profile=draft.profile)
        )

    async def confirm(self, memory_id: str, *, profile: str = "") -> Note:
        return _note(
            await self._api.send(
                "POST",
                f"/v1/memory/{segment(memory_id)}/confirm",
                profile=profile,
            )
        )

    async def correct(self, memory_id: str, title: str, body: str, *, profile: str = "") -> Note:
        return _note(
            await self._api.send(
                "POST",
                f"/v1/memory/{segment(memory_id)}/correct",
                body={"title": title, "body": body},
                profile=profile,
            )
        )

    async def forget(self, memory_id: str, *, profile: str = "") -> Note:
        return _note(
            await self._api.send(
                "POST",
                f"/v1/memory/{segment(memory_id)}/forget",
                profile=profile,
            )
        )


def _scope(*, kind: str, profile: str, session_id: str) -> dict[str, Any]:
    """Episode lives on this session; a fact lives on the profile unless it is about everyone."""
    if kind == "episode":
        return {"scope": "session", "profile": profile, "session_id": session_id}
    if profile:
        return {"scope": "profile", "profile": profile}
    return {"scope": "account"}


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
    "Block",
    "Draft",
    "HttpMemoryClient",
    "MemoryClient",
    "Note",
    "as_dict",
]
