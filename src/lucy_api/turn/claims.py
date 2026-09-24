"""A reply that says something was done, in a turn where nothing was.

Seen on the weakest model. Asked "Remember that I prefer tea over coffee", Lucy answered "Got
it. I've got that recorded -- tea over coffee." in a turn that ran no step at all: nothing was
remembered, and the person had no way to know. Asked to build a website, she said "Done. Your
calculator website is ready" after binding a capability and nothing more.

Whether a reply *says* something was done is a question about language, in whatever words and
whatever language the reply is in, so it is put to a model: the `claims` Laya decision. Code
only decides when it is worth asking -- a final reply, in a turn where no step did any real
work -- and what happens on a yes: `turn/loop.py` holds the reply back once and tells the model
it was not done. With decisions off, replies go out unchecked.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from weftai.decisions import Gate, noul

from lucy_api.decide.types import CLAIMS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from lucy_api.decide import Decisions
    from lucy_api.turn.loop import Round

BOOKKEEPING = frozenset({"capabilities", "help", "work"})
"""Capabilities whose steps are about Lucy's own tools, and never do anything for the person."""

UNBACKED = (
    "Your reply said something was done -- saved, written, changed or started -- but no step "
    "this turn did it, so it was not done. If it still needs doing, do it now with a plan. If "
    "not, tell the person plainly what you did and did not do."
)
"""What the model is told when a reply is held back."""

THRESHOLD = 0.9
"""How sure the decision has to be that a reply claims completed work before it is held back."""

QUESTION = (
    "Does this reply tell the person that something was done -- saved, recorded, remembered, "
    "written, changed, sent, started or set up?"
)


def did_work(rounds: Iterable[Round]) -> bool:
    """Whether any step this turn succeeded at something other than bookkeeping."""
    return any(
        step.status == "ok" and step.operation.split(".", 1)[0] not in BOOKKEEPING
        for round_ in rounds
        for step in round_.steps
    )


class ClaimCheck:
    """Whether a turn's final reply claims work nothing did, asked of the `claims` decision.

    `decide` is the turn's decisions, which also carry the person's words for this turn. A
    helper has none, and its replies go out unchecked. The decision can only hold a reply back,
    never let one through that something else stopped; in shadow mode what it would have done
    is measured and the reply goes out as before.
    """

    def __init__(self, decide: Decisions | None = None) -> None:
        self.decide = decide

    async def unbacked(self, text: str, rounds: Iterable[Round]) -> bool:
        decide = self.decide
        if decide is None or CLAIMS.id not in decide.enabled or did_work(rounds):
            return False
        state = json.dumps({"request": decide.request_text, "reply": text}, ensure_ascii=False)
        answers = await decide.ask(CLAIMS, state, [noul("claims_done", QUESTION)])
        gate: Gate[bool] = Gate(THRESHOLD, fail_open=False)
        if not gate.decide(answers, "claims_done", answers.noul("claims_done")):
            return False
        await decide.disagreed(CLAIMS, decided="hold", fallback="send")
        return decide.live(CLAIMS)


__all__ = ["BOOKKEEPING", "QUESTION", "THRESHOLD", "UNBACKED", "ClaimCheck", "did_work"]
