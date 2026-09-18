"""Account facts, as pinned lines, never as a ranked list mixed with memory.

User-api stores named fields and notes. Memory-api stores retrieval. Their ranking scores
are not comparable, so Lucy never merges the two lists. This client only reads the
always-load set (`pinned=true`) under the bare `user` audience, which is the unscoped
block: a `user.home` token would miss it, and a fused search would be the other way to
lose it — by burying it under a bm25 that means something else.

Sensitive entries are dropped here. Sensitivity is a volunteering hint, not access control,
and the standing prompt is exactly volunteering. A pack that dumped the raw page would
bring those lines up unprompted; filtering at the projection is what makes that
unrepresentable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.transport import Sibling, given, moment, rows, text

if TYPE_CHECKING:
    from datetime import datetime

    from lucy_api.packs.context import Http

SERVICE = "account"
AUDIENCE = "user"
PATH = "/v1/user/entries"
SENSITIVE = "sensitive"


@dataclass(frozen=True, slots=True)
class AccountFact:
    """One pinned field or note, stripped to a line the prompt or a tool result may carry."""

    id: str
    kind: str
    key: str
    line: str
    source: str = ""
    asserted_by: str = ""
    updated_at: datetime | None = None


class UserClient(Protocol):
    """The always-load surface, as feeds and notes.aboutMe call it."""

    async def pinned(self, *, profile: str = "") -> tuple[AccountFact, ...]: ...


class HttpUserClient:
    """User-api's pinned entries, over the one HTTP seam a capability is allowed."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def pinned(self, *, profile: str = "") -> tuple[AccountFact, ...]:
        payload = await self._api.send(
            "GET",
            PATH,
            params=given(pinned=True),
            profile=profile,
        )
        facts: list[AccountFact] = []
        for row in rows(payload, "entries"):
            fact = _fact(row)
            if fact is not None:
                facts.append(fact)
        return tuple(facts)


def _fact(row: Any) -> AccountFact | None:
    if not isinstance(row, dict):
        return None
    if text(row, "sensitivity") == SENSITIVE:
        return None
    if row.get("pinned") is False:
        return None
    kind = text(row, "entry_type", "field")
    key = text(row, "key")
    if kind == "note":
        line = text(row, "body")
    else:
        rendered = json.dumps(row.get("value"), ensure_ascii=False, separators=(",", ":"))
        line = f"{key}: {rendered}" if key else rendered
    compact = " ".join(line.split())
    if not compact:
        return None
    return AccountFact(
        id=text(row, "entry_id"),
        kind=kind,
        key=key,
        line=compact,
        source=text(row, "source"),
        asserted_by=text(row, "asserted_by"),
        updated_at=moment(row.get("updated_at")),
    )


def as_dict(fact: AccountFact) -> dict[str, Any]:
    """The projection a tool result is allowed to serialise. No account id, no raw value."""
    return {
        "id": fact.id,
        "kind": fact.kind,
        "key": fact.key,
        "line": fact.line,
        "source": fact.source,
        "asserted_by": fact.asserted_by,
    }


__all__ = [
    "AUDIENCE",
    "PATH",
    "AccountFact",
    "HttpUserClient",
    "UserClient",
    "as_dict",
]
