"""The band allocator: five budgets that cannot spend one another's money.

One pool is the amnesia bug. Give the whole window to whoever asks first and a single
40,000-token tool result evicts the person's pinned memory, and the model stops knowing who
it is talking to halfway through the task it was given. So each band is allocated against
its own ceiling and trimmed against its own ceiling, and the sections of one band are never
even looked at while another is being fitted. A tools-band overflow *cannot* reach a pinned
section, and that is an arithmetic property of the loop rather than a promise in a comment.

## What is given up, and in what order

`priority` reads the way a queue does, not the way a rating does: 0 is first class, so
sections are given up in descending priority and priority 0 is given up last. Ties go to
the section whose id sorts first. That rule is arbitrary and it is written down because the
alternative -- input order -- makes the result depend on the order in which a caller
happened to collect its sections, and two runs of the same session would then disagree
about what the model saw.

A section is shortened while what is left stays at or above `floor_tokens`, and dropped
whole below it, because half a fact is a lie rather than a shorter truth: three lines of a
diff read as the whole diff. That is also why a section whose allowance falls below its
floor is dropped rather than cut down to the floor and kept. Cutting it to the floor would
keep a stump of the *least* important section in the band and force the eviction of a more
important one, which is the priority order inverted.

## The middle is what goes

A trimmed section keeps its head and its tail, with the elision marked between them,
whenever there is enough text for both to be worth having. The head of a log, a diff or a
stack trace says what the thing is; the end is where the failure is. The middle is the
redundant part, and it is the only part a reader would have skimmed anyway. Below
`HEAD_TAIL_MINIMUM_CHARS` there is not room for two useful fragments, so the head alone is
kept -- two half-sentences and a marker are worse than one beginning. Cuts prefer a line
boundary, because half a line of JSON is noise that a model will try to parse anyway, but
only while the boundary is worth what it costs: see `LINE_BOUNDARY_MINIMUM_SHARE`.

## Nothing is lost quietly

Every shortened section carries the counts in the body the model reads *and* in
`Section.notice`, and every dropped section leaves exactly one line in `Assembled.notices`
naming it. Exactly one, and nothing else goes in there: a caller can therefore check that
the sections it gets back plus the notices account for every section it handed in, which is
the cheapest possible test that the allocator did not quietly lose something.

That check is only as good as the buckets it is counting, which is why every section's band
is resolved to a real `Band` before anything else happens. `Band` is a `StrEnum`, so a
section rebuilt from stored JSON arrives carrying the string `"tools"`; it compares equal to
`Band.tools` and is not the same object, and every bucket here is an identity test. An
unresolved band would therefore match no band at all, and the section would leave no
sections and no notice -- the exact disappearance this accounting exists to rule out.

## Two things this deliberately does not do

It does not reorder. The caller decides where a section sits, because ordering is about
prefix caching -- static before volatile -- and that is the assembler's decision, not the
budget's. And it does not trust `Section.tokens`: the producer may have counted with a
different tokenizer, or not counted at all, and an `Assembled` whose parts do not add up to
its total is a debugging trap. Every section is recounted with the counter in hand and
comes back carrying that number.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from lucy_api.context.types import Assembled, Band

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.context.types import Budget, Counter, Section

WRITABLE_BANDS: tuple[Band, ...] = tuple(band for band in Band if band is not Band.reserve)
"""Every band but the reserve, which exists to stay empty and is a caller error to fill."""

HEAD_TAIL_MINIMUM_CHARS = 160
"""Below this much room, a trim keeps the head only: two fragments would both be stumps."""

HEAD_SHARE = 0.6
"""How much of a two-fragment excerpt is head. The tail gets the rest, and it earns it."""

LINE_BOUNDARY_MINIMUM_SHARE = 0.5
"""How much of a fragment a cut must still keep after snapping it to a line boundary.

Preferring a line boundary assumes lines are short relative to the fragment. Minified JSON,
a base64 blob and a single-line stack frame break that assumption: the only newline can sit
three characters into a fragment worth four thousand tokens, and snapping to it would spend
the whole allowance on a header and then report -- accurately, uselessly -- that two tokens
of ten thousand were shown. A ragged edge is a much smaller lie than an empty section, so
below this share the cut lands where the character budget ran out.
"""

MINIMUM_CONTENT_TOKENS = 1
"""A section shortened to nothing is a label, not a truth, so it is dropped and confessed."""


def allocate(sections: Sequence[Section], budget: Budget, counter: Counter) -> Assembled:
    """Fit `sections` into `budget`, one band at a time, confessing every loss.

    Raises:
        ValueError: if any section is in `Band.reserve`, or names a band that does not
            exist. Both are checked across the whole input before a single token is
            counted, so a caller that got it wrong pays nothing to find out.
    """
    checked = [_checked(section) for section in sections]
    entries = [
        _Entry(order=order, section=section, cost=counter.count(section.body))
        for order, section in enumerate(checked)
    ]
    survivors: list[_Entry] = []
    notices: list[str] = []
    by_band: dict[Band, int] = {}
    for band, allocation in _allocations(budget).items():
        in_band = [entry for entry in entries if entry.section.band is band]
        kept, total, dropped = _fit(in_band, allocation, counter)
        survivors.extend(kept)
        notices.extend(dropped)
        by_band[band] = total

    survivors.sort(key=lambda entry: entry.order)
    return Assembled(
        sections=tuple(replace(entry.section, tokens=entry.cost) for entry in survivors),
        by_band=by_band,
        total=sum(by_band.values()),
        notices=tuple(notices),
    )


@dataclass(slots=True)
class _Entry:
    """One section while it is being fitted, plus the two facts that change about it."""

    order: int
    section: Section
    cost: int
    dropped: bool = False


def _checked(section: Section) -> Section:
    """The section with its band resolved to a real `Band`, or a refusal that names the fix.

    Resolving rather than simply rejecting a bare string is deliberate: `Band.tools` and
    `"tools"` mean the same thing to everyone except the `is` comparisons this module and
    `Assembled.band` are built out of, and turning a correct answer in the wrong spelling
    into an error would buy nothing. A band that names nothing at all is a different matter
    and is refused, because the alternative is the silent disappearance described in the
    module docstring.
    """
    try:
        band = Band(section.band)
    except ValueError:
        known = ", ".join(member.value for member in Band)
        msg = (
            f"Section {section.id!r} is in band {section.band!r}, which is not a band. Use "
            f"one of lucy_api.context.types.Band: {known}. A band is how a section finds the "
            "ceiling it is trimmed against, so an unknown one belongs to no band and would "
            "be left out of the prompt with nothing in the notices to say it ever arrived."
        )
        raise ValueError(msg) from None
    if band is Band.reserve:
        msg = (
            f"Section {section.id!r} was handed to the allocator in the reserve band. "
            "The reserve is kept empty on purpose: it is the room this turn's reply and "
            "one more large tool result have to fit into. Put the section in the band it "
            "belongs to -- tools for a result, pinned for what must survive a compaction."
        )
        raise ValueError(msg)
    return section if section.band is band else replace(section, band=band)


def _allocations(budget: Budget) -> dict[Band, int]:
    """Each writable band's ceiling in tokens, scaled down together if they over-promise.

    `Budget` cannot stop a caller writing shares that sum above one, and taken literally
    those shares would overflow the window the reserve exists to protect. Scaling every band
    by the same factor keeps the bands independent -- the factor is a property of the budget
    and of nobody's sections -- and makes a mis-configured budget cost a smaller prompt
    rather than a request the provider refuses outright.
    """
    ceilings = {band: max(0, budget.allocation(band)) for band in WRITABLE_BANDS}
    promised = sum(ceilings.values())
    usable = max(0, budget.usable)
    if promised <= usable:
        return ceilings
    return {band: ceiling * usable // promised for band, ceiling in ceilings.items()}


def _fit(
    entries: list[_Entry], allocation: int, counter: Counter
) -> tuple[list[_Entry], int, list[str]]:
    """Bring one band down to its ceiling, and report what that cost.

    The entries are mutated in place, which is the point of `_Entry`: the caller keeps the
    input order it built them in while this function works through a different one.
    """
    total = sum(entry.cost for entry in entries)
    notices: list[str] = []
    for entry in sorted(entries, key=_given_up_before):
        if total <= allocation:
            break
        # A section that costs nothing reclaims nothing, so giving it up would be a loss
        # taken for free.
        if entry.cost == 0:
            continue
        section = entry.section
        floor = max(section.floor_tokens, MINIMUM_CONTENT_TOKENS)
        excerpt = _excerpt(section.body, entry.cost, entry.cost - (total - allocation), counter)
        shown = excerpt.shown if excerpt is not None else 0
        if excerpt is None or shown < floor:
            entry.dropped = True
            total -= entry.cost
            notices.append(_drop_notice(section, entry.cost, shown, floor))
            continue
        entry.section = section.with_body(
            excerpt.body, excerpt.tokens, notice=_trim_notice(section, shown, entry.cost)
        )
        total -= entry.cost - excerpt.tokens
        entry.cost = excerpt.tokens

    survivors = [entry for entry in entries if not entry.dropped]
    # Add it up again rather than trust the running total: what `by_band` reports and what
    # the surviving sections say they cost are then the same number by construction.
    return survivors, sum(entry.cost for entry in survivors), notices


def _given_up_before(entry: _Entry) -> tuple[int, str, int]:
    """Descending priority, then id, then arrival -- the last only to break a repeated id."""
    return (-entry.section.priority, entry.section.id, entry.order)


@dataclass(frozen=True, slots=True)
class _Excerpt:
    """A shortened body, what it costs with its confession, and what it actually shows."""

    body: str
    tokens: int
    shown: int


def _excerpt(text: str, original: int, allowance: int, counter: Counter) -> _Excerpt | None:
    """As much of `text` as `allowance` holds, or None if not even the confession fits.

    `original` is `text` already counted, and must be at least one because it is what the
    characters-per-token ratio is derived from. That ratio is only a first guess: the loop
    measures what it built and shrinks until it really fits, so the ceiling holds for a
    tokenizer that disagrees with the estimate as well as for the estimate itself.
    """
    per_token = len(text) / original
    chars = max(0, min(int(allowance * per_token), len(text) - 1))
    while True:
        head, tail = _fragments(text, chars)
        shown = counter.count("\n".join(piece for piece in (head, tail) if piece))
        body = "\n".join(piece for piece in (head, _marker(shown, original), tail) if piece)
        tokens = counter.count(body)
        if tokens <= allowance:
            return _Excerpt(body=body, tokens=tokens, shown=shown)
        if chars <= 0:
            return None
        # Both terms shrink `chars`, so this terminates: the ratio jumps most of the way
        # down in one step, and the minus one guarantees progress when rounding would not.
        chars = max(0, min(chars - 1, chars * allowance // tokens))


def _fragments(text: str, chars: int) -> tuple[str, str]:
    """Split a character budget into the head and the tail that will be kept."""
    if chars <= 0:
        return "", ""
    if chars < HEAD_TAIL_MINIMUM_CHARS:
        return _head(text, chars), ""
    head_chars = max(1, int(chars * HEAD_SHARE))
    return _head(text, head_chars), _tail(text, chars - head_chars)


def _head(text: str, chars: int) -> str:
    """The first `chars` characters, backed up to a line boundary when one is affordable."""
    fragment = text[:chars]
    boundary = fragment.rfind("\n")
    # `boundary` is also the length of what backing up would keep, so this one comparison
    # asks both questions at once: is there a boundary, and does it leave enough behind.
    return fragment[:boundary] if boundary >= _worth_snapping_to(chars) else fragment


def _tail(text: str, chars: int) -> str:
    """The last `chars` characters, advanced to a line boundary when one is affordable."""
    fragment = text[len(text) - chars :]
    # `find` answers -1 when there is no boundary, and advancing past -1 advances past
    # nothing -- which is what "no boundary" should do anyway, so it needs no second test.
    boundary = fragment.find("\n")
    kept = len(fragment) - boundary - 1
    return fragment[boundary + 1 :] if kept >= _worth_snapping_to(chars) else fragment


def _worth_snapping_to(chars: int) -> int:
    """The fewest characters a cut may keep and still be allowed to land on a line boundary.

    At least one, so that a boundary at the very edge of a fragment never yields an empty
    head or an empty tail -- which is how a whole allowance gets spent on nothing.
    """
    return max(1, int(chars * LINE_BOUNDARY_MINIMUM_SHARE))


def _marker(shown: int, original: int) -> str:
    """The elision, in the body the model reads, with the counts spelled out."""
    return f"[... showing {shown:,} of {original:,} tokens ...]"


def _trim_notice(section: Section, shown: int, original: int) -> str:
    return (
        f"{section.id} was shortened to fit the {section.band.value} band: "
        f"showing {shown:,} of {original:,} tokens."
    )


def _drop_notice(section: Section, cost: int, shown: int, floor: int) -> str:
    return (
        f"{section.id} was dropped whole from the {section.band.value} band: it needs "
        f"{cost:,} tokens, only {shown:,} would fit, and that is below its floor of "
        f"{floor:,} tokens."
    )


__all__ = [
    "HEAD_SHARE",
    "HEAD_TAIL_MINIMUM_CHARS",
    "LINE_BOUNDARY_MINIMUM_SHARE",
    "MINIMUM_CONTENT_TOKENS",
    "WRITABLE_BANDS",
    "allocate",
]
