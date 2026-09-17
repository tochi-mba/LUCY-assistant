"""The vocabulary of Lucy's context window.

A model's entire experience of the world is the token sequence it is handed. So "what does
Lucy know right now?" is not a question about databases; it is a question about what the
assembler put in, in what order, and at what cost. This module is the contract every other
part of that answer is written against.

## The prompt is five zones, ordered by how often they change

Ordering by volatility is not tidiness, it is the whole economics of the system. A provider
caches a *prefix*: everything up to a breakpoint is charged at a fraction of the input
price, and the first byte that differs from last turn ends the cache. So anything rewritten
every turn must sit **after** everything that is not.

    zone 0  static    identity, behaviour, tool idiom, safety      changes on deploy
    zone 1  slow      persona, pinned memory, capability names     changes on connect
    zone 2  history   turns and compactions                        grows at the end
    zone 3  live      the state block                              REWRITTEN EVERY TURN
    zone 4  input     what the person just said                    new

The live state block is the one that keeps Lucy current, and it is therefore the one that
must never be placed in the system prompt. Put it at the front and every turn pays full
price for the whole prefix, which on a long session is the difference between a
conversation that is affordable and one that is not. Placed last, it costs its own length
and nothing else.

## Bands are budgets, and they are enforced separately

One pool would let a single large tool result evict the person's pinned memory, which is
the failure that makes an assistant feel like it has amnesia halfway through a task. Each
band is allocated independently and trimmed independently, so nothing can spend another
band's money.

## Every trim is confessed

A section that was shortened says so, in the text the model reads, with the counts. Silent
truncation is worse than no truncation: the model cannot tell that it is reasoning from a
fragment, and neither can anybody debugging it afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


class Band(StrEnum):
    """The five independently budgeted regions of the window."""

    system = "system"
    """Identity, behaviour, the tool idiom, safety. Stable across a whole session."""

    pinned = "pinned"
    """What must survive every compaction: the person, active goals, the live state."""

    history = "history"
    """The conversation, as projected through whatever compactions are active."""

    tools = "tools"
    """Tool results. The largest band and the first one reclaimed."""

    reserve = "reserve"
    """Left empty on purpose: this turn's output, plus room for one more large result."""


DEFAULT_SHARES: Mapping[Band, float] = {
    Band.system: 0.04,
    Band.pinned: 0.03,
    Band.history: 0.30,
    Band.tools: 0.50,
    Band.reserve: 0.13,
}
"""Fractions of the effective window. They sum to one; `reserve` is never written to."""


@dataclass(frozen=True, slots=True)
class Budget:
    """How many tokens each band may spend, for one model's effective window."""

    window: int
    shares: Mapping[Band, float] = field(default_factory=lambda: dict(DEFAULT_SHARES))

    def allocation(self, band: Band) -> int:
        """The ceiling for one band, in tokens."""
        return int(self.window * self.shares.get(band, 0.0))

    @property
    def usable(self) -> int:
        """Everything except the reserve, which exists to stay empty."""
        return self.window - self.allocation(Band.reserve)


@dataclass(frozen=True, slots=True)
class Section:
    """One addressable piece of the prompt, with its own floor and its own confession.

    `priority` orders trimming, lowest first to survive: a section with priority 0 is given
    up last. `floor_tokens` is the length below which shortening it is worse than dropping
    it, because half a fact is a lie rather than a shorter truth.
    """

    id: str
    band: Band
    body: str
    tokens: int
    priority: int = 50
    floor_tokens: int = 0
    title: str = ""
    truncated: bool = False
    notice: str = ""

    def with_body(self, body: str, tokens: int, *, notice: str = "") -> Section:
        """A shortened copy that says it was shortened."""
        return Section(
            id=self.id,
            band=self.band,
            body=body,
            tokens=tokens,
            priority=self.priority,
            floor_tokens=self.floor_tokens,
            title=self.title,
            truncated=True,
            notice=notice or self.notice,
        )


@dataclass(frozen=True, slots=True)
class Assembled:
    """The exact prompt, plus the accounting that explains it.

    This is what `GET /v1/sessions/{id}/context` returns. A person debugging a strange
    answer should be able to read precisely what the model was given, and see which band
    was full at the time.
    """

    sections: tuple[Section, ...]
    by_band: Mapping[Band, int]
    total: int
    notices: tuple[str, ...] = ()

    def band(self, band: Band) -> tuple[Section, ...]:
        """Every section in one band, in prompt order."""
        return tuple(section for section in self.sections if section.band is band)

    def text(self) -> str:
        """The prompt as the model sees it."""
        return "\n\n".join(section.body for section in self.sections if section.body)


# --------------------------------------------------------------------------------------
# The live state block
#
# Each snapshot below is a fact about *now* that the model would otherwise have to ask for,
# guess at, or carry forward from a turn that has since been compacted away. Every one of
# them is delta-aware where a delta is what drives behaviour: what a model does about an
# agent is decided by whether it just finished, not by the fact that it exists.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """Where this conversation stands."""

    id: str
    profile: str
    title: str
    turn_number: int
    permission_mode: str
    incognito: bool = False


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """What the model has left, so it can spend it deliberately.

    Telling a model its own context position changes what it does: it writes a note before
    an eviction rather than after one, and it stops opening large pages when there is no
    room to read them.
    """

    used: int
    window: int
    reclaimable: int = 0
    last_compaction_turn: int | None = None

    @property
    def percent(self) -> int:
        return int(100 * self.used / self.window) if self.window else 0


@dataclass(frozen=True, slots=True)
class AgentSnapshot:
    """One child run, as its parent needs to see it.

    `objective` is the plain sentence written when the child was spawned, never a
    restatement of its arguments: a parent deciding whether to wait or to carry on is
    answering a question about intent.
    """

    id: str
    role: str
    objective: str
    status: str
    depth: int = 1
    elapsed_seconds: float = 0.0
    progress: str = ""
    finished_since_last_turn: bool = False


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    """One entry in the shared journal, which is how siblings see each other's work.

    The journal is the cheapest form of coordination there is: no context is transferred
    between agents, and yet each can see what the others have claimed and completed.
    """

    id: str
    title: str
    status: str
    claimed_by: str = ""
    blocked_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TopicSnapshot:
    """One cluster of memories, as a line in the index.

    The index is the point. Carrying every memory would cost more than it is worth and
    would bury the useful ones; carrying none leaves the model unable to know that it knows
    anything. A title, a sentence and a count let it choose a topic and ask for the rest.
    """

    id: str
    title: str
    summary: str
    count: int
    last_seen: datetime | None = None
    trust: str = "stated"
    unread: int = 0


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """The filesystem the model is working in, and what moved in it."""

    path: str
    ready: bool
    changed_files: tuple[str, ...] = ()
    last_checkpoint: str = ""
    expires_in_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """One capability and whether it just appeared or just went away."""

    id: str
    title: str
    state: str
    changed: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PendingSnapshot:
    """Everything waiting on somebody else, so the model stops rather than spins."""

    approvals: tuple[str, ...] = ()
    elicitations: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()

    @property
    def any(self) -> bool:
        return bool(self.approvals or self.elicitations or self.connections)


@dataclass(frozen=True, slots=True)
class FailureSnapshot:
    """A recent failure, kept so the model stops retrying what cannot work.

    A model that cannot see that it has already called this operation twice and been
    refused twice will call it a third time. This is the cheapest loop-breaker available.
    """

    operation: str
    count: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LiveState:
    """Everything that is true right now and was not true, or not known, last turn."""

    now: datetime
    session: SessionSnapshot
    budget: BudgetSnapshot
    agents: tuple[AgentSnapshot, ...] = ()
    tasks: tuple[TaskSnapshot, ...] = ()
    topics: tuple[TopicSnapshot, ...] = ()
    capabilities: tuple[CapabilitySnapshot, ...] = ()
    workspace: WorkspaceSnapshot | None = None
    pending: PendingSnapshot = field(default_factory=PendingSnapshot)
    failures: tuple[FailureSnapshot, ...] = ()

    @property
    def running_agents(self) -> tuple[AgentSnapshot, ...]:
        return tuple(agent for agent in self.agents if agent.status == "running")


class Trust(StrEnum):
    """How much weight a claim in the context deserves.

    Nothing here is an instruction. `untrusted` exists because a memory distilled from a
    web page is a place an attacker can write, and permanence is precisely what makes a
    memory store worth attacking.
    """

    stated = "stated"
    observed = "observed"
    inferred = "inferred"
    untrusted = "untrusted"


@dataclass(frozen=True, slots=True)
class Claim:
    """One piece of content that came from somewhere other than the person or Lucy.

    Rendered in the third person, past tense, with its provenance inline and a closing line
    that says it is not an instruction. Never concatenated into the system prompt, and
    never stripped of provenance to save tokens -- fetch fewer claims instead.
    """

    body: str
    source: str
    trust: Trust = Trust.stated
    asserted_by: str = ""
    recorded_at: datetime | None = None


class Counter(Protocol):
    """How tokens are counted.

    A four-characters-per-token estimate is good enough for budgeting and free; a real
    tokenizer is correct and slow. Both satisfy this, so a deployment can choose, and the
    band allocator never has to know which it got.
    """

    def count(self, text: str) -> int: ...


__all__ = [
    "DEFAULT_SHARES",
    "AgentSnapshot",
    "Assembled",
    "Band",
    "Budget",
    "BudgetSnapshot",
    "CapabilitySnapshot",
    "Claim",
    "Counter",
    "FailureSnapshot",
    "LiveState",
    "PendingSnapshot",
    "Section",
    "SessionSnapshot",
    "TaskSnapshot",
    "TopicSnapshot",
    "Trust",
    "WorkspaceSnapshot",
]
