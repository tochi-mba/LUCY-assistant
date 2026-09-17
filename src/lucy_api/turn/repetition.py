"""Noticing that a model is going round in circles, and telling it so.

A model that calls the same operation with the same arguments three times is not being
thorough. It has usually misread a result, or is retrying something that failed for a
reason it cannot see, and left alone it will keep going until the iteration cap stops it --
having spent the whole turn learning nothing.

The fix is cheap and it is not a hard block. The loop notices the repeat and puts a
sentence in front of the model saying what it already tried and what happened. That is
usually enough, because the model is not being stubborn, it has simply lost track. Only
when it ignores that does the operation get dropped for the rest of the turn.

## Why a notice before a block

A hard block on the second identical call breaks legitimate work: polling a job, reading a
file that another step just wrote, retrying something that is genuinely transient. Those
look identical to a loop from the outside and are told apart only by whether the answer is
changing -- which is exactly what the notice lets the model check for itself.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field

NOTICE_AT = 2
"""Calls with identical arguments before the model is told it is repeating itself."""

DROP_AT = 4
"""Before the operation is withdrawn for the rest of the turn. Deliberately well above
`NOTICE_AT`: the notice should have room to work, and dropping a tool the model needs is a
worse failure than letting it try twice more."""


def fingerprint(operation: str, arguments: object) -> str:
    """One string standing for "this exact call".

    Arguments are canonicalised -- sorted keys, no incidental whitespace -- so that two
    calls a person would call identical are identical here too. A model that reorders its
    own JSON keys between attempts is still repeating itself.
    """
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{operation}\x1f{canonical}".encode()).hexdigest()
    return digest[:16]


@dataclass(slots=True)
class Repetition:
    """What this turn has already tried, and what it was told."""

    counts: Counter[str] = field(default_factory=Counter)
    outcomes: dict[str, str] = field(default_factory=dict)
    operations: dict[str, str] = field(default_factory=dict)
    dropped: set[str] = field(default_factory=set)

    def record(self, operation: str, arguments: object, outcome: str) -> None:
        """Remember one call and how it went."""
        key = fingerprint(operation, arguments)
        self.counts[key] += 1
        self.operations[key] = operation
        self.outcomes[key] = outcome

    def seen(self, operation: str, arguments: object) -> int:
        return self.counts[fingerprint(operation, arguments)]

    def notice_for(self, operation: str, arguments: object) -> str:
        """The sentence to put in front of the model, or nothing when it is not repeating.

        It names the outcome as well as the count, because "you already did this" without
        "and it said X" leaves the model with no more information than it had before.
        """
        key = fingerprint(operation, arguments)
        count = self.counts[key]
        if count < NOTICE_AT:
            return ""
        outcome = self.outcomes.get(key, "the same result")
        return (
            f"You have already called {operation} with these arguments {count} times, and it "
            f"returned: {outcome}. Calling it again will return the same thing. Either use "
            f"that result, change the arguments, or say what is blocking you."
        )

    def should_drop(self, operation: str, arguments: object) -> bool:
        """Whether this operation has earned being withdrawn for the rest of the turn."""
        return self.counts[fingerprint(operation, arguments)] >= DROP_AT

    def drop(self, operation: str) -> None:
        self.dropped.add(operation)

    def is_dropped(self, operation: str) -> bool:
        return operation in self.dropped

    def summary(self) -> str:
        """What was withdrawn, for the state block's `trouble` group."""
        if not self.dropped:
            return ""
        names = ", ".join(sorted(self.dropped))
        return f"withdrawn for the rest of this turn after repeated identical calls: {names}"


__all__ = ["DROP_AT", "NOTICE_AT", "Repetition", "fingerprint"]
