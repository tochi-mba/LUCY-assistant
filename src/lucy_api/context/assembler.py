"""Putting the window together, in the order that decides what it costs.

Everything else in this package produces pieces. This is the one place that decides where
each piece goes, and that decision is worth more than any of the pieces.

## The order is the design

    zone 0  system     identity, behaviour, the tool idiom, safety
    zone 1  pinned     the person, their goals, what is connected
    zone 2  history    the conversation, projected through active compactions
    zone 2  tools      the results those turns produced
    zone 3  live       the state block

A provider caches a prefix. Everything up to a breakpoint is charged at a fraction of the
input price, and the first byte that differs from the previous turn ends the cache. Zones 0
and 1 change when somebody connects a capability or edits a note -- rarely. Zone 2 only ever
grows at its end. Zone 3 is rewritten every single turn.

So zone 3 goes **last**, after the history, and that is the whole reason this module exists
as something other than a list comprehension. The instinct is to put the current state in
the system prompt, where a human reader would look for it. Do that and every turn pays full
price for the entire prefix, which on a long session is the difference between a
conversation that is affordable and one that is not.

Note what this means: a section's **band** and its **position** are different things. The
live block is budgeted in `pinned`, because it must survive whatever else is competing for
room, but it is positioned at the end. The allocator keeps the order it is given, so the
order this module builds is the order the model reads.

## Nothing is lost quietly

The allocator confesses every trim and every drop, and those confessions come back in
`Assembled.notices`. They are not decoration: a prompt that quietly dropped the person's
goals looks exactly like a prompt that never had any, both to the model and to whoever is
trying to work out why the answer was strange. `GET /v1/sessions/{id}/context` returns this
whole object for that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lucy_api.context.bands import allocate
from lucy_api.context.tokens import default_counter
from lucy_api.context.types import Band, Budget, Section
from lucy_api.prompt.sections import PromptContext, render_all

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from lucy_api.context.types import Assembled, Counter

DEFAULT_WINDOW = 200_000
"""A sane effective window when nobody has said which model this session runs on."""


@dataclass(frozen=True, slots=True)
class Window:
    """One turn's raw material, in the order it will be read.

    `history` and `tools` arrive already built, because how a turn becomes a section is a
    question about transcripts and projections, and this module is only about placement and
    price. `live` arrives already rendered for the same reason.
    """

    prompt: PromptContext = field(default_factory=PromptContext)
    history: Sequence[Section] = ()
    tools: Sequence[Section] = ()
    live: Section | None = None
    overrides: Mapping[str, str] | None = None
    disabled: Collection[str] | None = None


def assemble(
    window: Window,
    *,
    budget: Budget | None = None,
    counter: Counter | None = None,
) -> Assembled:
    """Build one turn's context: the exact sections, in order, priced and trimmed.

    The returned object is the answer to "what did the model actually see", which is the
    first question worth asking about any surprising reply.
    """
    pricing = counter if counter is not None else default_counter()
    allowance = budget if budget is not None else Budget(window=DEFAULT_WINDOW)

    ordered: list[Section] = list(
        render_all(
            window.prompt,
            overrides=window.overrides,
            disabled=window.disabled,
            counter=pricing,
        )
    )
    ordered.extend(_in_band(window.history, Band.history))
    ordered.extend(_in_band(window.tools, Band.tools))
    if window.live is not None:
        # Last, and budgeted in `pinned`. See the module docstring: position and band are
        # separate decisions, and this is the section where they disagree on purpose.
        ordered.append(_placed(window.live, Band.pinned))

    return allocate(ordered, allowance, pricing)


def _in_band(sections: Sequence[Section], band: Band) -> list[Section]:
    """Sections a caller supplied, forced into the band this zone is budgeted from.

    A caller who builds a tool result and leaves it in the default band would have it
    competing with the system prompt for a four-percent allowance. Rather than refuse, the
    zone decides, because the zone is the thing that actually knows.
    """
    return [_placed(section, band) for section in sections]


def _placed(section: Section, band: Band) -> Section:
    if section.band is band:
        return section
    return Section(
        id=section.id,
        band=band,
        body=section.body,
        tokens=section.tokens,
        priority=section.priority,
        floor_tokens=section.floor_tokens,
        title=section.title,
        truncated=section.truncated,
        notice=section.notice,
    )


__all__ = ["DEFAULT_WINDOW", "PromptContext", "Window", "assemble"]
