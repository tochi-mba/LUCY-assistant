"""A reply that says something was done, in a turn where nothing was.

Seen, twice, on the weakest model. Asked "Remember that I prefer tea over coffee", Lucy
answered "Got it. I've got that recorded -- tea over coffee." in a turn that ran no step at
all: nothing was remembered, and the person had no way to know. Asked to build a website, she
said "Done. Your calculator website is ready" after binding a capability and nothing more.

A structural check, not a prompt: a model that makes the claim has already read the prompt.
When a turn's final reply claims a completed change and no step this turn did anything --
nothing succeeded outside the bookkeeping capabilities -- the reply is held back and the model
is asked once to either do it or say it was not done. Once, because the phrase list is English
and a heuristic: a false match costs one round, and a second answer is taken as given.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lucy_api.turn.loop import Round

BOOKKEEPING = frozenset({"capabilities", "help", "work"})
"""Capabilities whose steps are about Lucy's own tools, and never do anything for the person."""

CLAIMED = re.compile(
    r"\b(?:i(?:'ve| have)|i just|(?:it|that)(?:'s| is| has)(?: been)?)"
    r"(?: now| just| already| also)?\s+"
    r"(?:got (?:that|it|this) )?"
    r"(?:saved|recorded|remembered|noted|stored|written|wrote|created|updated|changed|edited|"
    r"deleted|removed|added|set|scheduled|sent|installed|built|made|started|fixed|moved|renamed|"
    r"copied|uploaded|downloaded|connected|turned (?:on|off))\b"
    r"|\A\W*(?:all )?done\b",
    re.IGNORECASE,
)
"""A first-person claim that a change was made, or a reply that opens by saying it is done."""

UNBACKED = (
    "Your reply said something was done -- saved, written, changed or started -- but no step "
    "this turn did it, so it was not done. If it still needs doing, do it now with a plan. If "
    "not, tell the person plainly what you did and did not do."
)
"""What the model is told when a claim of that kind is held back."""


def unbacked(text: str, rounds: Iterable[Round]) -> bool:
    """Whether `text` claims a change that nothing this turn made."""
    for round_ in rounds:
        for step in round_.steps:
            if step.status == "ok" and step.operation.split(".", 1)[0] not in BOOKKEEPING:
                return False
    return CLAIMED.search(text) is not None


__all__ = ["BOOKKEEPING", "CLAIMED", "UNBACKED", "unbacked"]
