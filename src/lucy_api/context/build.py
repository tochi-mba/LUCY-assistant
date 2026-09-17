"""One turn's context, end to end.

Everything else in this package does one job well. This is the function the turn loop calls,
and it exists so that the order of those jobs is written down once rather than re-derived
at every call site:

1. **Gather** the live state, concurrently, tolerating any source being absent.
2. **Render** it into a single block, inside its own share of the pinned band.
3. **Project** the transcript through whatever compactions are active.
4. **Assemble** everything in reading order and price it.

The only slow step is the first one, and it is slow because it is six network calls;
those already run together. The other three are arithmetic over data that is in memory, so
there is nothing to overlap them with -- the projection is CPU work, and running it in a
thread would cost more in scheduling than it could ever save.

## Why the live block gets a share rather than the whole band

The pinned band is what survives a compaction: the person, their goals, and the state of
the world right now. Those compete. Handing the live block the entire band would let a
session with thirty running agents push out the notes that say who the person is; handing
it a fixed number of tokens would leave it starved on a small model and wasteful on a large
one. A share of the band scales with the window and still leaves room for the rest.

The block is trimmed to that share *before* the allocator sees it. That is deliberate: the
allocator's job is to be a blunt instrument, cutting text at line boundaries, while the
renderer knows that dropping the whole `capabilities` group costs less than half-rendering
the memory index. Whoever knows more should cut first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from lucy_api.context.assembler import DEFAULT_WINDOW, Window, assemble
from lucy_api.context.feeds import FeedRequest, claims_from, gather_feeds
from lucy_api.context.policy import apply_policy
from lucy_api.context.projection import project
from lucy_api.context.sources import Sources, gather_live_state
from lucy_api.context.state import render_state
from lucy_api.context.tokens import default_counter
from lucy_api.context.types import Band, Budget, FeedSnapshot
from lucy_api.prompt.sections import PromptContext

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from lucy_api.context.feeds import FeedSource
    from lucy_api.context.policy import Flags
    from lucy_api.context.projection import Compaction, Item
    from lucy_api.context.sources import StateRequest
    from lucy_api.context.types import Assembled, Counter, Section

LIVE_SHARE = 0.6
"""How much of the pinned band the live state may take. The rest is the person."""


@dataclass(frozen=True, slots=True)
class Turn:
    """Everything one turn needs from storage, already read."""

    items: Sequence[Item] = ()
    compactions: Sequence[Compaction] = ()
    tools: Sequence[Section] = ()
    prompt: PromptContext = field(default_factory=PromptContext)
    overrides: Mapping[str, str] | None = None
    disabled: Collection[str] | None = None


@dataclass(frozen=True, slots=True)
class Built:
    """The window, and the two things worth knowing about how it was made."""

    context: Assembled
    live_tokens: int
    replaced_items: int

    @property
    def notices(self) -> tuple[str, ...]:
        return self.context.notices


@dataclass(frozen=True, slots=True)
class Live:
    """Where the state of the world comes from, and what a person allows into the prompt.

    Three things that always travel together: what to ask, who to ask, and which answers
    are allowed to be shown. Separating them at the call site invites a caller to pass two
    of the three and get a prompt that is quietly missing a capability's worth of context.
    """

    sources: Sources | None = None
    feeds: Sequence[FeedSource] = ()
    flags: Flags | None = None


async def build_context(
    request: StateRequest,
    turn: Turn,
    *,
    live_from: Live | None = None,
    budget: Budget | None = None,
    counter: Counter | None = None,
) -> Built:
    """Assemble one turn's context. Nothing here raises because a source was unavailable."""
    inputs = live_from if live_from is not None else Live()
    sources, feed_sources, flags = inputs.sources, inputs.feeds, inputs.flags
    pricing = counter if counter is not None else default_counter()
    allowance = budget if budget is not None else Budget(window=DEFAULT_WINDOW)

    live_limit = int(allowance.allocation(Band.pinned) * LIVE_SHARE)
    collected = apply_policy(
        await gather_feeds(
            FeedRequest(
                profile=request.session.profile,
                session_id=request.session.id,
                incognito=False,
            ),
            tuple(feed_sources),
        ),
        flags,
        incognito=request.session.incognito,
    )
    prompt = turn.prompt
    if collected.standing:
        prompt = replace(
            prompt,
            feeds=collected.standing,
            notes=prompt.notes or claims_from(collected.standing),
        )
    state = await gather_live_state(request, sources if sources is not None else Sources())
    if collected.live or collected.failures:
        state = replace(
            state,
            feeds=tuple(
                FeedSnapshot(id=feed.id, title=feed.title, lines=feed.lines)
                for feed in collected.live
            ),
            failures=(*state.failures, *collected.failures),
        )
    projection = project(turn.items, turn.compactions, counter=pricing)

    live = render_state(state, limit=live_limit, counter=pricing)
    context = assemble(
        Window(
            prompt=prompt,
            history=projection.sections,
            tools=turn.tools,
            live=live,
            overrides=turn.overrides,
            disabled=turn.disabled,
        ),
        budget=allowance,
        counter=pricing,
    )
    return Built(
        context=context,
        live_tokens=live.tokens,
        replaced_items=projection.replaced,
    )


__all__ = ["LIVE_SHARE", "Built", "PromptContext", "Sources", "Turn", "build_context"]
