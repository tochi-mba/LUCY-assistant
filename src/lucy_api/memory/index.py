"""The live-state source that turns Memory-api's topic index into snapshots.

Ranking, trust filtering and the honest cut all live in `topics.py`. This module is the
adapter the turn path actually calls: one HTTP list, then those rules, then the shape the
live-state renderer already understands. Keeping that join here means `prepare_turn` never
has to know how a topic is scored, and a test can drive the index with `FakeTopics` without
standing up a sibling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from lucy_api.context.types import Trust
from lucy_api.memory.topics import Topic, select_topics

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.clients.memory import TopicCard
    from lucy_api.context.types import TopicSnapshot
    from lucy_api.memory.topics import TopicSource


class TopicListing(Protocol):
    """The one Memory-api read the live index needs."""

    async def topics(self, *, profile: str = "") -> tuple[TopicCard, ...]: ...


class _OnePerTopic:
    """A topic is one slot in `memory_retrieval_limit`, whatever its summary costs.

    The live-state renderer still prices the lines in tokens and will drop what will not
    fit. This counter is only the person's "show me N topics" setting, so a long summary
    cannot steal a neighbour's slot before the renderer has seen the set.
    """

    def count(self, text: str) -> int:  # noqa: ARG002 - Counter; cost is one slot
        return 1


@dataclass(frozen=True, slots=True)
class MemoryIndex:
    """One profile's topic index, fetched for every turn that is allowed to remember."""

    client: TopicListing
    profile: str
    limit: int = 8
    incognito: bool = False

    async def fetch(self, session_id: str) -> Sequence[TopicSnapshot]:  # noqa: ARG002
        """The ranked, trusted prefix. Incognito is empty rather than a differently shaped miss."""
        if self.incognito:
            return ()
        cards = await self.client.topics(profile=self.profile)
        selection = select_topics(
            [_topic_from_card(card) for card in cards],
            limit=max(1, self.limit),
            counter=_OnePerTopic(),
            now=datetime.now(UTC),
        )
        return selection.snapshots()


@dataclass(frozen=True, slots=True)
class StoredIndex:
    """The same cut, over a `TopicSource` that never leaves the process.

    Tests of ranking already seed `FakeTopics`. Wiring those seeds through this adapter is
    what proves the live-state source applies the same cut the unit tests pinned.
    """

    source: TopicSource
    account_id: str
    profile: str
    limit: int = 8

    async def fetch(self, session_id: str) -> Sequence[TopicSnapshot]:  # noqa: ARG002
        topics = await self.source.list_topics(self.account_id, profile=self.profile)
        selection = select_topics(
            topics,
            limit=max(1, self.limit),
            counter=_OnePerTopic(),
            now=datetime.now(UTC),
        )
        return selection.snapshots()


def _topic_from_card(card: TopicCard) -> Topic:
    try:
        trust = Trust(card.trust)
    except ValueError:
        trust = Trust.untrusted
    return Topic(
        id=card.id,
        key=card.key or card.id,
        title=card.title,
        summary=card.summary,
        count=card.count,
        importance=card.importance,
        first_seen=card.first_seen,
        last_seen=card.last_seen,
        trust=trust,
        unread=card.unread,
    )


__all__ = ["MemoryIndex", "StoredIndex", "TopicListing"]
