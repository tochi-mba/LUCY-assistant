"""How a sibling publishes what the model should already know, without writing the prompt.

Persona notes, a now-playing line, a workspace cwd: each is a fact an API owns, and each
used to look like a special case that belonged in the system prompt. Putting live facts in
the system prompt is the expensive instinct this module exists to refuse. A provider caches
a prefix; anything rewritten every turn that sits in that prefix makes the whole prompt
full price again. So a sibling does not get to choose a slot. It publishes a **feed** --
keyed lines, a volatility, a version -- and Lucy places it.

**Standing** feeds (who you are, pinned notes) live in zone 1, the cached prefix, framed
the way persona notes already are. They update when ``version`` changes, which is how a
correction becomes visible next turn without rewriting the prefix every turn.

**Live** feeds (what is playing, which shell is running) join the live state block after
the history. They are never concatenated into the instruction block.

The sibling never sends markdown or a system-prompt fragment. It sends keyed lines. Lucy
scrubs them, drops keys the person turned off, frames standing ones as reported claims,
and budgets live ones as groups in the state block. A missing source costs that feed, not
the turn.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from lucy_api.context.types import Claim, FailureSnapshot, Trust

FEED_ID = re.compile(r"^[a-z][a-z0-9]{0,31}$")
"""Product names: ``persona``, ``music``, ``workspace``. Not a service, not a port."""

ENTRY_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
"""One field inside a feed: ``now_playing``, ``device``, ``pid``."""

MAX_FEEDS = 8
MAX_ENTRIES = 32
MAX_LINE_CHARS = 240

TRUST_WORDS = {
    "stated": Trust.stated,
    "observed": Trust.observed,
    "inferred": Trust.inferred,
    "untrusted": Trust.untrusted,
}


class Volatility(StrEnum):
    """How often this feed is allowed to change, which is what decides where it sits."""

    standing = "standing"
    """Zone 1. Cached with the prefix. Updates when ``version`` changes, not every turn."""

    live = "live"
    """The live state block. Rewritten every turn, never in the system prompt."""


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One keyed fact. The key is what a setting toggles; the line is what the model reads."""

    key: str
    line: str


@dataclass(frozen=True, slots=True)
class Feed:
    """One sibling's contribution, as facts rather than as prompt text."""

    id: str
    title: str
    volatility: Volatility = Volatility.standing
    version: str = ""
    entries: tuple[FeedEntry, ...] = ()
    trust: Trust = Trust.stated
    personal: bool = True
    ceiling_tokens: int = 400

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(entry.line for entry in self.entries)

    def as_claims(self) -> tuple[Claim, ...]:
        """Standing lines as the person section already knows how to frame them."""
        return tuple(
            Claim(body=entry.line, source=self.id, trust=self.trust) for entry in self.entries
        )

    def with_entries(self, entries: tuple[FeedEntry, ...]) -> Feed:
        return Feed(
            id=self.id,
            title=self.title,
            volatility=self.volatility,
            version=self.version,
            entries=entries,
            trust=self.trust,
            personal=self.personal,
            ceiling_tokens=self.ceiling_tokens,
        )


@dataclass(frozen=True, slots=True)
class FeedRequest:
    """Who the feed is for. Profile, not account, is what persona files notes against."""

    profile: str
    session_id: str = ""
    account_id: str = ""
    incognito: bool = False


@dataclass(frozen=True, slots=True)
class CollectedFeeds:
    """Standing and live, already split, plus the sources that failed this turn."""

    standing: tuple[Feed, ...] = ()
    live: tuple[Feed, ...] = ()
    failures: tuple[FailureSnapshot, ...] = ()

    @property
    def all(self) -> tuple[Feed, ...]:
        return (*self.standing, *self.live)


class FeedSource(Protocol):
    """One sibling, or one in-process stand-in, that can publish feeds for a profile."""

    name: str

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]: ...


@dataclass(frozen=True, slots=True)
class StaticFeeds:
    """A source that already knows its lines. Tests, and anything that does not need HTTP."""

    name: str
    feeds: tuple[Feed, ...] = ()
    delay: float = 0.0

    async def fetch(self, _request: FeedRequest) -> tuple[Feed, ...]:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.feeds


class BreakingFeeds:
    """A deployed source that is down. Absence is silent; this is reported."""

    name = "broken"

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or TimeoutError("took too long")

    async def fetch(self, _request: FeedRequest) -> tuple[Feed, ...]:
        raise self.error


def parse_document(payload: Any) -> tuple[Feed, ...]:
    """The standard sibling document, read forgivingly.

    ``{ "feeds": [ { "id", "title", "volatility", "version", "entries"|"lines",
    "trust", "personal" } ] }``. A bare feed object, or a list of them, is accepted so a
    service that has not wrapped the array yet still participates. Unknown fields are
    dropped: a projection that only takes named keys cannot smuggle a new one into the
    prompt.
    """
    raw = _feed_rows(payload)
    parsed: list[Feed] = []
    seen: set[str] = set()
    for item in raw:
        feed = _one(item)
        if feed is None or feed.id in seen:
            continue
        seen.add(feed.id)
        parsed.append(feed)
        if len(parsed) >= MAX_FEEDS:
            break
    return tuple(parsed)


def claims_from(feeds: tuple[Feed, ...]) -> tuple[Claim, ...]:
    """Every standing line, in feed order, for the person section's existing renderer."""
    claims: list[Claim] = []
    for feed in feeds:
        if feed.volatility is Volatility.standing:
            claims.extend(feed.as_claims())
    return tuple(claims)


def split_feeds(feeds: tuple[Feed, ...]) -> CollectedFeeds:
    """Partition already-fetched feeds without another round trip."""
    return CollectedFeeds(
        standing=tuple(feed for feed in feeds if feed.volatility is Volatility.standing),
        live=tuple(feed for feed in feeds if feed.volatility is Volatility.live),
    )


async def gather_feeds(
    request: FeedRequest, sources: tuple[FeedSource, ...] = ()
) -> CollectedFeeds:
    """Fetch every source concurrently. Absence is silent; failure costs that source."""
    if not sources:
        return CollectedFeeds()
    trouble: list[FailureSnapshot] = []
    batches = await asyncio.gather(
        *(_fetch(source, request, trouble) for source in sources),
        return_exceptions=False,
    )
    merged = _first_wins(tuple(feed for batch in batches for feed in batch))
    visible = tuple(feed for feed in merged if not (request.incognito and feed.personal))
    collected = split_feeds(visible)
    return CollectedFeeds(standing=collected.standing, live=collected.live, failures=tuple(trouble))


async def _fetch(
    source: FeedSource, request: FeedRequest, trouble: list[FailureSnapshot]
) -> tuple[Feed, ...]:
    try:
        return await source.fetch(request)
    except Exception as exc:
        trouble.append(
            FailureSnapshot(
                operation=source.name,
                count=1,
                detail=f"unavailable ({type(exc).__name__}); omitted from this turn",
            )
        )
        return ()


def _first_wins(feeds: tuple[Feed, ...]) -> tuple[Feed, ...]:
    seen: set[str] = set()
    kept: list[Feed] = []
    for feed in feeds:
        if feed.id in seen:
            continue
        seen.add(feed.id)
        kept.append(feed)
    return tuple(kept)


def _feed_rows(payload: Any) -> tuple[Any, ...]:
    if isinstance(payload, list):
        return tuple(payload)
    if not isinstance(payload, dict):
        return ()
    listed = payload.get("feeds")
    if isinstance(listed, list):
        return tuple(listed)
    if payload.get("id") or payload.get("lines") is not None or payload.get("entries") is not None:
        return (payload,)
    return ()


def _one(raw: Any) -> Feed | None:
    if not isinstance(raw, dict):
        return None
    identity = str(raw.get("id") or "").strip().lower()
    if not FEED_ID.match(identity):
        return None
    volatility = _volatility(raw.get("volatility"))
    if volatility is None:
        return None
    entries = _entries(raw)
    if not entries:
        return None
    trust_word = str(raw.get("trust") or "stated")
    personal = raw.get("personal")
    ceiling = raw.get("ceiling_tokens")
    try:
        ceiling_tokens = int(ceiling) if ceiling is not None else 400
    except (TypeError, ValueError):
        ceiling_tokens = 400
    return Feed(
        id=identity,
        title=str(raw.get("title") or identity).strip() or identity,
        volatility=volatility,
        version=str(raw.get("version") or ""),
        entries=entries,
        trust=TRUST_WORDS.get(trust_word, Trust.untrusted),
        personal=True if personal is None else bool(personal),
        ceiling_tokens=max(ceiling_tokens, 1),
    )


def _volatility(value: Any) -> Volatility | None:
    if value is None or value == "":
        return Volatility.standing
    word = str(value).strip().lower()
    if word == Volatility.standing:
        return Volatility.standing
    if word == Volatility.live:
        return Volatility.live
    return None


def _entries(raw: dict[str, Any]) -> tuple[FeedEntry, ...]:
    listed = raw.get("entries")
    if isinstance(listed, list):
        return _from_entries(listed)
    return _from_lines(raw.get("lines"))


def _from_entries(listed: list[Any]) -> tuple[FeedEntry, ...]:
    found: list[FeedEntry] = []
    seen: set[str] = set()
    for item in listed:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip().lower()
        line = _line(item.get("line"))
        if not ENTRY_KEY.match(key) or not line or key in seen:
            continue
        seen.add(key)
        found.append(FeedEntry(key=key, line=line))
        if len(found) >= MAX_ENTRIES:
            break
    return tuple(found)


def _from_lines(value: Any) -> tuple[FeedEntry, ...]:
    if not isinstance(value, list):
        return ()
    found: list[FeedEntry] = []
    for index, item in enumerate(value):
        line = _line(item)
        if not line:
            continue
        found.append(FeedEntry(key=f"line_{index}", line=line))
        if len(found) >= MAX_ENTRIES:
            break
    return tuple(found)


def _line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    return compact[:MAX_LINE_CHARS] if compact else ""


__all__ = [
    "BreakingFeeds",
    "CollectedFeeds",
    "Feed",
    "FeedEntry",
    "FeedRequest",
    "FeedSource",
    "StaticFeeds",
    "Volatility",
    "claims_from",
    "gather_feeds",
    "parse_document",
    "split_feeds",
]
