"""What a decision is worth to Lucy, and the three gates it has to clear.

A decision is never authority. It may make Lucy ask, escape, hold, or prefer a different
option among ones the deterministic path could also have chosen; it may not grant, promote
trust, or skip an approval. Every use in this package states its direction, and
:class:`Use` records it so a test can read the whole set back.

Three gates stand between a question and its answer being acted on:

1. ``lucy.decisions`` -- the master, off until a person turns it on.
2. the use's own setting -- each judgement is enabled separately.
3. ``lucy.decision_shadow_mode`` -- on by default, and while it is on every enabled use
   runs, is measured, and is emitted as an event whose answer is then discarded.

Shadow mode is the reason this is safe to try. A person who turns the master on gets the
events and byte-identical behaviour, watches how often the decision and the deterministic
path disagree on their own traffic, and only then turns global shadow mode off for the
enabled uses they have evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

Direction = Literal["tighten", "advisory"]
"""`tighten` can only restrict an action. `advisory` changes context selection or adds
a suggestion, without granting authority or executing an action."""


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


CAPABILITIES = Use(
    "capabilities",
    "decision_capabilities",
    "advisory",
    "ordinary capability discovery",
    "Preload relevant eligible capabilities.",
)
MEMORY = Use(
    "memory",
    "decision_memory",
    "advisory",
    "recency and importance ranking",
    "Promote relevant trusted memory topics.",
)
RECOVERY = Use(
    "recovery",
    "decision_recovery",
    "advisory",
    "deterministic loop guards",
    "Suggest reconsidering consecutive failed attempts.",
)

CLAIMS = Use(
    "claims",
    "decision_claims",
    "tighten",
    "replies go out unchecked",
    "Hold back a reply that claims work no step did.",
)

USES: tuple[Use, ...] = (CAPABILITIES, MEMORY, RECOVERY, CLAIMS)

__all__ = [
    "CAPABILITIES",
    "CLAIMS",
    "MEMORY",
    "RECOVERY",
    "USES",
    "Direction",
    "Skip",
    "Use",
]
