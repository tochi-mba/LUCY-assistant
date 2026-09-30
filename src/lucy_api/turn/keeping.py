"""Something worth keeping was said, and the turn is about to end without keeping it.

Lucy is told to keep what is worth keeping without being asked: a preference, a fact about the
person's life or work, how they want things done. A model can still let one go by, most often
the weakest model on a message that was mostly about something else, and then the person says
it again next week.

Whether a message held something worth keeping is a question about what the person meant, in
whatever words they typed it, so it is put to a model: the `keeping` Laya decision. Code only
decides when it is worth asking -- a final reply, in a conversation that may keep things at all
-- and what happens on a yes: `turn/loop.py` holds the reply back once and tells the model what
it may have missed. The model still decides whether to keep it, and the memory write policy and
the person's approval still decide whether it is kept. With decisions off, nothing is asked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from weftai.decisions import Gate, noul

from lucy_api.decide.types import KEEPING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lucy_api.decide import Decisions
    from lucy_api.turn.loop import Round

UNKEPT = (
    "The person told you something that may be worth keeping for later conversations, and "
    "nothing this turn kept it. If it is worth keeping, keep it now with a plan and then "
    "answer. If it is not, answer as you were going to."
)
"""What the model is told when a reply is held back."""

THRESHOLD = 0.9
"""How sure the decision has to be before a reply is held back for it."""

QUESTION = (
    "Did the person say something about themselves, their work or how they want things done "
    "that is worth remembering in later conversations, and that none of these steps kept?"
)


class Keeping:
    """Whether a turn is ending with something worth keeping left unkept.

    `able` is whether this conversation may keep anything: it is not incognito, remembering
    is not switched off, and notes can be reached. A helper has no decisions, and nothing is
    asked for it.
    """

    def __init__(self, decide: Decisions | None = None, *, able: bool = True) -> None:
        self.decide = decide
        self.able = able

    async def unkept(self, rounds: Iterable[Round]) -> bool:
        decide = self.decide
        if decide is None or not self.able or KEEPING.id not in decide.enabled:
            return False
        if not decide.request_text:
            return False
        ran = [
            {"operation": step.operation, "for": step.note, "status": step.status}
            for round_ in rounds
            for step in round_.steps
        ]
        state = json.dumps({"request": decide.request_text, "steps": ran}, ensure_ascii=False)
        answers = await decide.ask(KEEPING, state, [noul("keeping_missed", QUESTION)])
        gate: Gate[bool] = Gate(THRESHOLD, fail_open=False)
        if not gate.decide(answers, "keeping_missed", answers.noul("keeping_missed")):
            return False
        await decide.disagreed(KEEPING, decided="reconsider", fallback="send")
        return decide.live(KEEPING)


__all__ = ["QUESTION", "THRESHOLD", "UNKEPT", "Keeping"]
