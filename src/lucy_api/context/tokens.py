"""Counting tokens often enough that the counting itself has to be cheap.

The assembler asks "what does this cost?" of every section on every turn, and the honest
answer -- run the model's real tokenizer -- costs a measurable slice of the turn it is
trying to budget for. So the default here is an estimate: four characters to a token, which
is close enough to decide what to trim and free to compute. `Counter` in
`lucy_api.context.types` is a Protocol precisely so that a deployment which needs the exact
number can hand the allocator a real tokenizer without the allocator ever learning which it
got.

The estimate is blunt and it is wrong in both directions: dense code, JSON and non-Latin
scripts cost more per character, ordinary English prose a little less. It decides how much
of a section survives, never what a provider is promised, so being a few per cent out costs
a few per cent of a band rather than a rejected request. A budget that has to be exact
should reserve headroom rather than expect precision from this module.

`Cached` exists because the expensive case is the boring one. The system prompt is
byte-identical on every turn of a session; so is the persona, and so is every tool result
already sitting in the history. Counting them again each turn is pure waste whichever
counter is underneath. The cache is bounded and evicts least-recently-used first because
the text it is keyed on is mostly model- and tool-authored, which is to say unbounded: an
unbounded cache on that is a memory leak with a long fuse. It is keyed on the text itself
rather than a digest because hashing the text costs a pass over the same characters the
estimate would have walked anyway, and the dictionary holds a reference to a string the
section is already keeping alive rather than a second copy of it.
"""

from __future__ import annotations

from collections import OrderedDict
from math import ceil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lucy_api.context.types import Counter

CHARS_PER_TOKEN = 4
"""The ratio everybody uses for English. See the module docstring for what it costs."""

DEFAULT_CAPACITY = 512
"""Enough for a session's stable prefix and its recent tool results, and no more."""


class Estimate:
    """Length divided by four, rounded up. An estimate, and it says so."""

    def count(self, text: str) -> int:
        """Round up, so that an empty string is free and one character still costs one."""
        return ceil(len(text) / CHARS_PER_TOKEN)


class Cached:
    """Another counter's answers, remembered for the texts that keep coming back.

    Bounded, and oldest-use-first when the bound is reached. The alternative -- remember
    everything -- looks harmless until a session pastes a repository into the history.
    """

    def __init__(self, inner: Counter, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            msg = (
                f"A cache capacity of {capacity} cannot hold anything. Pass at least 1, or "
                "use the counter being wrapped directly when caching is not wanted."
            )
            raise ValueError(msg)
        self._inner = inner
        self._capacity = capacity
        self._counts: OrderedDict[str, int] = OrderedDict()

    def count(self, text: str) -> int:
        """The wrapped counter's answer, computed at most once per distinct text."""
        # `is not None` rather than a truth test: an empty section legitimately costs zero,
        # and counting it again every turn because zero is falsy would be a silly bug.
        remembered = self._counts.get(text)
        if remembered is not None:
            self._counts.move_to_end(text)
            return remembered
        counted = self._inner.count(text)
        self._counts[text] = counted
        if len(self._counts) > self._capacity:
            self._counts.popitem(last=False)
        return counted

    @property
    def size(self) -> int:
        """How many distinct texts are remembered right now."""
        return len(self._counts)


def default_counter() -> Counter:
    """What a caller should use when it has no opinion: the estimate, cached."""
    return Cached(Estimate())


def fits(text: str, limit: int, counter: Counter) -> bool:
    """Whether `text` can be afforded at `limit` tokens.

    A caller with a yes-or-no question should ask it this way rather than compare counts by
    hand, because the comparison is where an off-by-one lands.
    """
    return counter.count(text) <= limit


__all__ = ["CHARS_PER_TOKEN", "DEFAULT_CAPACITY", "Cached", "Estimate", "default_counter", "fits"]
