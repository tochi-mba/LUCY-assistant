"""What a decision is worth to Lucy, and the three gates it has to clear.

A decision is never authority. It may make Lucy ask, escape, hold, or prefer a different
option among ones the deterministic path could also have chosen; it may not grant, promote
trust, or skip an approval. Every use in this package states its direction, and
:class:`Use` records it so a test can read the whole set back.

Three gates stand between a question and its answer being acted on, and the default answer
at each is no:

1. ``lucy.decisions`` -- the master, off until a person turns it on.
2. the use's own setting -- each judgement is enabled separately.
3. ``lucy.decision_shadow_mode`` -- on by default, and while it is on every enabled use
   runs, is measured, and is emitted as an event whose answer is then discarded.

Shadow mode is the reason this is safe to try. A person who turns the master on gets the
events and byte-identical behaviour, watches how often the decision and the deterministic
path disagree on their own traffic, and only then turns shadow off for a use they believe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Direction = Literal["tighten", "advisory"]
"""`tighten` is the rule. `advisory` is for a use that only adds a sentence and changes
nothing, and every one of those has to say so out loud."""


class Skip(StrEnum):
    """Why a decision was not acted on. Every value is a fail-open path."""

    OFF = "off"
    """`lucy.decisions` is off, or this use's own setting is."""
    NO_DECIDER = "no_decider"
    """No decision model is configured, so there was nothing to ask."""
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    TURN_BUDGET = "turn_budget"
    """`lucy.decision_max_per_turn` is spent; every later use falls open for this turn."""


@dataclass(frozen=True, slots=True)
class Use:
    """One judgement Lucy delegates, and the promises that come with it.

    `setting` is the per-use key under the `lucy` namespace. `fallback` names, in one phrase,
    what happens without the decision -- it is what the docs page and the event carry, and
    writing it down is what stops a use shipping without a deterministic path behind it.
    """

    id: str
    setting: str
    direction: Direction
    fallback: str
    summary: str

    @property
    def tightens(self) -> bool:
        return self.direction == "tighten"


TOPIC = Use(
    id="topic",
    setting="decision_topic",
    direction="tighten",
    fallback="Jaccard overlap against the same topics",
    summary="Which existing topic a new memory joins, or none of them.",
)
"""Only ever routes to a topic the word-overlap pass could also have chosen, or abstains to a
new one. It never touches a candidate that carries a key: a key settles the question on its
own, and folding one key into another is a decision about the taxonomy."""


USES: tuple[Use, ...] = (TOPIC,)
"""Every declared use. The settings catalogue, the docs page and the tests all read this."""


__all__ = ["TOPIC", "USES", "Direction", "Skip", "Use"]
