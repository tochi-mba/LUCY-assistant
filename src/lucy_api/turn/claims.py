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

import json
import re
from typing import TYPE_CHECKING

from weftai.decisions import Gate, noul

from lucy_api.decide.types import CLAIMS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lucy_api.decide import Decisions
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


THRESHOLD = 0.9
"""How sure a decision has to be that a reply claims completed work before it is held back."""

QUESTION = (
    "Does this reply tell the person that an action was completed -- something saved, "
    "recorded, remembered, written, changed, sent, started or set up?"
)


def did_work(rounds: Iterable[Round]) -> bool:
    """Whether any step this turn succeeded at something other than bookkeeping."""
    return any(
        step.status == "ok" and step.operation.split(".", 1)[0] not in BOOKKEEPING
        for round_ in rounds
        for step in round_.steps
    )


def unbacked(text: str, rounds: Iterable[Round]) -> bool:
    """Whether `text` claims, in words the phrase list knows, a change nothing this turn made."""
    return not did_work(rounds) and CLAIMED.search(text) is not None


class ClaimCheck:
    """The phrase list, with a decision in front of it where one is live.

    The phrase list is English and names the shapes seen; it cannot know every way of saying
    that something is done, in every language. A decision can -- so, when `decision_claims`
    is enabled, a reply the phrase list passed is asked about. The decision can only add a
    hold, never remove one: a direction of `tighten` means it may make Lucy stricter and never
    looser, so a reply the phrase list held back is held back whatever the answer. In shadow
    mode the disagreement is measured and the reply goes out as before.
    """

    def __init__(self, decide: Decisions | None = None) -> None:
        self.decide = decide

    async def unbacked(self, text: str, rounds: Iterable[Round]) -> bool:
        if did_work(rounds):
            return False
        if CLAIMED.search(text) is not None:
            return True
        decide = self.decide
        if decide is None or CLAIMS.id not in decide.enabled:
            return False
        answers = await decide.ask(
            CLAIMS, json.dumps({"reply": text}, ensure_ascii=False), [noul("claims_done", QUESTION)]
        )
        gate: Gate[bool] = Gate(THRESHOLD, fail_open=False)
        if not gate.decide(answers, "claims_done", answers.noul("claims_done")):
            return False
        await decide.disagreed(CLAIMS, decided="hold", fallback="send")
        return decide.live(CLAIMS)


__all__ = [
    "BOOKKEEPING",
    "CLAIMED",
    "QUESTION",
    "THRESHOLD",
    "UNBACKED",
    "ClaimCheck",
    "did_work",
    "unbacked",
]
