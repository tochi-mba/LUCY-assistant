"""Noticing that a model is going round in circles, and telling it so.

A model that makes the same call and gets the same answer, again, is not being thorough. It
has usually misread a result, or is retrying something that failed for a reason it cannot see,
and left alone it will keep going until the turn's budget stops it -- having spent the whole
turn learning nothing.

The fix is a sentence, not a block. The loop tells the model what it already tried and that
the answer has not changed. That is usually enough, because the model is not being stubborn,
it has lost track. What stops a model that ignores it is the turn's own budget of rounds and
calls (`turn/stop.py`), which is one mechanism rather than two.

## Why the answer has to be the same

Polling a job, reading a file another step just wrote and retrying something transient all
make the same call twice, and they are work. What tells them from a loop is whether the answer
is changing, so that is what is counted: the same call returning the same thing, in a row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

NOTICE_AT = 2
"""How many times in a row a call must return the same thing before the model is told."""


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
    """One turn's record of which calls came back the same, and how many times running."""

    answers: dict[str, str] = field(default_factory=dict)
    runs: dict[str, int] = field(default_factory=dict)

    def record(self, operation: str, arguments: object, outcome: str) -> str:
        """Remember one call and how it went. Returns what to tell the model, or nothing.

        The sentence leaves the answer out: it is in the transcript already, and repeating a
        page of it here would be the same waste the sentence is about.
        """
        call = fingerprint(operation, arguments)
        answer = hashlib.sha256(outcome.encode()).hexdigest()
        same = self.answers.get(call) == answer
        self.answers[call] = answer
        self.runs[call] = self.runs.get(call, 0) + 1 if same else 1
        times = self.runs[call]
        if times < NOTICE_AT:
            return ""
        return (
            f"You have called {operation} with the same arguments {times} times running and "
            "it returned the same thing each time. Calling it again will not change the "
            "answer: use the result you have, change the arguments, or say what is blocking "
            "you."
        )


__all__ = ["NOTICE_AT", "Repetition", "fingerprint"]
