"""Turning an append-only transcript into the history the model is shown.

The transcript is never edited. Not when a conversation is compacted, not when it is
forked, not when a summary turns out to be bad. What changes is the **projection**: the
view of that log which is computed fresh every time a prompt is built.

That distinction is the difference between a system you can debug and one you cannot. If
compaction rewrote the log, then "why did it think that?" would be unanswerable the moment
a summary dropped the detail that explains it, and a bad summary would be permanent. As a
projection, a compaction is a row that can be deactivated, regenerated with a different
prompt, or simply read alongside the turns it replaced.

## Two rules the projection must never break

**A range is replaced whole or not at all.** A compaction that covered half a turn would
leave the model reading an answer to a question it cannot see, or -- much worse -- a
`tool_result` whose `tool_use` has been summarised away. Providers reject that outright,
and it is the commonest way a hand-rolled compactor fails. So a covered range is snapped
outwards to turn boundaries before anything is dropped.

**The newest turns are given up last.** Everything here competes for one band, and when it
does not all fit, what a conversation can most afford to lose is its oldest surviving
detail. Priority descends with age for exactly that reason, and a summary outranks the
turns it replaced, because losing the summary would lose all of them at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lucy_api.context.types import Band, Section

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from lucy_api.context.types import Counter

SUMMARY_PRIORITY = 10
"""Below every ordinary turn: a summary stands for many of them, so it goes last."""

OLDEST_PRIORITY = 90
NEWEST_PRIORITY = 20
"""Turns are spread across this range by age. Both ends sit above the summary and below the
live state, which the assembler places in another band entirely."""


@dataclass(frozen=True, slots=True)
class Item:
    """One entry of the transcript, flattened to what the prompt needs.

    `turn_id` is here and not optional-in-spirit: it is what makes a boundary a boundary.
    An item with no turn belongs to no turn and is never inside a compacted range.
    """

    id: str
    seq: int
    role: str
    body: str
    turn_id: str | None = None
    kind: str = "message"


@dataclass(frozen=True, slots=True)
class Compaction:
    """A summary standing in for a closed range of the transcript.

    `covers_from` and `covers_to` are inclusive sequence numbers. They are what the
    compactor *asked* for; what is actually replaced is that range snapped outwards to whole
    turns, which is computed here rather than trusted from the row.
    """

    seq: int
    summary: str
    covers_from: int
    covers_to: int
    active: bool = True


@dataclass(frozen=True, slots=True)
class Projection:
    """The history as it will be read, plus what it is standing in for."""

    sections: tuple[Section, ...] = ()
    replaced: int = 0
    summaries: int = 0
    notices: tuple[str, ...] = field(default_factory=tuple)


def project(
    items: Sequence[Item],
    compactions: Sequence[Compaction] = (),
    *,
    counter: Counter,
) -> Projection:
    """Build the history band: summaries in place of the turns they cover, then the rest."""
    ordered = sorted(items, key=lambda item: item.seq)
    active = [compaction for compaction in compactions if compaction.active]
    ranges = [(_snap(compaction, ordered)) for compaction in sorted(active, key=lambda c: c.seq)]

    covered: set[int] = set()
    placed: list[tuple[int, Section]] = []
    notices: list[str] = []

    for compaction, (low, high) in zip(sorted(active, key=lambda c: c.seq), ranges, strict=True):
        inside = [item.seq for item in ordered if low <= item.seq <= high]
        fresh = [sequence for sequence in inside if sequence not in covered]
        if not fresh:
            # A later compaction already covers everything this one did. Keeping both would
            # show the same turns summarised twice, which reads as two separate events.
            notices.append(f"compaction {compaction.seq} covers nothing not already summarised")
            continue
        covered.update(inside)
        body = _summary_body(compaction, low, high, len(inside))
        placed.append(
            (
                low,
                Section(
                    id=f"history.summary.{compaction.seq}",
                    band=Band.history,
                    body=body,
                    tokens=counter.count(body),
                    priority=SUMMARY_PRIORITY,
                    floor_tokens=counter.count(body),
                    title="Earlier in this conversation",
                ),
            )
        )

    survivors = [item for item in ordered if item.seq not in covered]
    placed.extend(_turn_sections(survivors, counter))
    # Chronological, keyed on the sequence number each piece stands at: a summary sits where
    # the range it replaced began, so the conversation still reads forwards.
    placed.sort(key=lambda entry: entry[0])
    sections = [section for _, section in placed]
    return Projection(
        sections=tuple(sections),
        replaced=len(covered),
        summaries=sum(1 for part in sections if part.id.startswith("history.summary.")),
        notices=tuple(notices),
    )


def _snap(compaction: Compaction, items: Sequence[Item]) -> tuple[int, int]:
    """Widen a covered range to whole turns.

    A `tool_result` separated from its `tool_use` is rejected by every provider, and it is
    the single commonest defect in a hand-written compactor. Snapping outwards can only ever
    summarise slightly more than was asked; snapping inwards, or not snapping, can produce a
    prompt that cannot be sent at all.
    """
    low, high = compaction.covers_from, compaction.covers_to
    touched = {
        item.turn_id for item in items if item.turn_id is not None and low <= item.seq <= high
    }
    if not touched:
        return low, high
    members = [item.seq for item in items if item.turn_id in touched]
    return min(low, *members), max(high, *members)


def _summary_body(compaction: Compaction, low: int, high: int, replaced: int) -> str:
    """A summary that says what it stands for, so nothing looks like it never happened."""
    return (
        f"[summary of {replaced} earlier entries, sequence {low} to {high}. "
        "The full transcript is unchanged and can be read back.]\n"
        f"{compaction.summary}"
    )


def _turn_sections(items: Iterable[Item], counter: Counter) -> list[tuple[int, Section]]:
    """One section per surviving item, priced, with age turned into a priority."""
    rows = list(items)
    if not rows:
        return []
    # With a single entry there is no age to spread: it is the newest thing there is, and
    # clamping the span to one would price it as the oldest and give it up first.
    span = len(rows) - 1
    sections = []
    for position, item in enumerate(rows):
        share = (span - position) / span if span else 0.0
        priority = NEWEST_PRIORITY + round(share * (OLDEST_PRIORITY - NEWEST_PRIORITY))
        body = f"{item.role}: {item.body}"
        sections.append(
            (
                item.seq,
                Section(
                    id=f"history.item.{item.id}",
                    band=Band.history,
                    body=body,
                    tokens=counter.count(body),
                    priority=priority,
                    title=item.kind,
                ),
            )
        )
    return sections


__all__ = [
    "NEWEST_PRIORITY",
    "OLDEST_PRIORITY",
    "SUMMARY_PRIORITY",
    "Compaction",
    "Item",
    "Projection",
    "project",
]
