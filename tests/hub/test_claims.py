"""A final reply that says something was done, in a turn where nothing was, is held back once.

Found by `lucy eval` on clyde:haiku: asked "Remember that I prefer tea over coffee", Lucy said
"Got it. I've got that recorded -- tea over coffee." in a turn that ran no step at all. Whether
a reply claims something was done is a question about language, so a model answers it: the
`claims` Laya decision. These tests stand in for Laya with a decider that answers as told.
"""

from __future__ import annotations

from typing import Any

from test_turn_loop import Prompts, Transcript, executor, ok_result
from weftai.decisions import Answer, Answers

from lucy_api.decide import Decisions
from lucy_api.decide.types import CLAIMS, USES
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.settings.policy import TurnPolicy
from lucy_api.turn.claims import QUESTION, UNBACKED, ClaimCheck
from lucy_api.turn.loop import Round, Step, Turn, run_turn
from lucy_api.turn.stop import Termination

FALSE = "Got it. Tea over coffee."
REQUEST = "remmeber i like tea more then coffe"
REMEMBER = {"steps": [{"id": "tea", "op": "notes.remember", "input": {"title": "tea"}}]}
BIND = {"steps": [{"id": "use", "op": "capabilities.use", "input": {"id": "notes"}}]}


class Laya:
    """A decider that says yes to the replies it is given, and no to every other."""

    def __init__(self, *claims: str, probability: float = 0.97) -> None:
        self.claims = set(claims)
        self.probability = probability
        self.asked: list[tuple[str, Any]] = []

    async def decide(self, state: str, questions: Any) -> Answers:
        self.asked.append((state, questions))
        yes = any(claim in state for claim in self.claims)
        return Answers([Answer(q.id, "noul", yes, self.probability) for q in questions])


def _check(
    laya: Laya, *, shadow: bool = False, enabled: bool = True
) -> tuple[ClaimCheck, list[str]]:
    events: list[str] = []

    async def emit(name: str, _fields: dict[str, Any]) -> None:
        events.append(name)

    decisions = Decisions(laya, enabled=[CLAIMS.id] if enabled else [], shadow=shadow, emit=emit)
    decisions.request_text = REQUEST
    return ClaimCheck(decisions), events


async def _turn(
    check: ClaimCheck, *script: Any, results: tuple[dict[str, Any], ...] = ()
) -> tuple[Any, list[str], Prompts]:
    transcript, prompts = Transcript(), Prompts()
    outcome = await run_turn(
        Turn(
            provider=ScriptedProvider(list(script)),
            assemble=prompts.assemble,
            append=transcript.append,
            execute=executor(*results) if results else None,
            claims=check,
        )
    )
    said = [content for kind, _role, content in transcript.items if kind == "message"]
    return outcome, said, prompts


async def test_a_claim_with_no_step_behind_it_is_held_back_and_the_work_done() -> None:
    """The bug, named: this reply reached the person, and nothing was remembered."""
    laya = Laya(FALSE)
    check, events = _check(laya)
    remembered = ok_result(id="tea", operation="notes.remember", data={"id": "mem_1"})

    outcome, said, prompts = await _turn(
        check,
        speaks(FALSE),
        plans(REMEMBER),
        speaks("Saved: you prefer tea."),
        results=(remembered,),
    )

    assert outcome.termination is Termination.success
    assert said == ["Saved: you prefer tea."]
    assert UNBACKED in prompts.notices[1]
    [(state, [question])] = laya.asked
    assert REQUEST in state, "the judgment sees what was asked, typos and all"
    assert FALSE in state
    assert question.prompt == QUESTION
    assert "lucy.decision.disagreed" in events


async def test_a_reply_is_held_back_once_and_a_second_is_taken_as_given() -> None:
    check, _events = _check(Laya(FALSE))
    outcome, said, _prompts = await _turn(check, speaks(FALSE), speaks(FALSE))
    assert outcome.termination is Termination.success
    assert said == [FALSE]


async def test_after_a_step_that_did_real_work_nothing_is_asked() -> None:
    laya = Laya(FALSE)
    check, _events = _check(laya)
    remembered = ok_result(id="tea", operation="notes.remember", data={"id": "mem_1"})
    _outcome, said, _prompts = await _turn(
        check, plans(REMEMBER), speaks(FALSE), results=(remembered,)
    )
    assert said == [FALSE]
    assert laya.asked == []


async def test_after_only_bookkeeping_steps_the_claim_is_still_judged() -> None:
    """Binding a capability does nothing for the person: "Done. Your website is ready"."""
    check, _events = _check(Laya("It's ready."))
    bound = ok_result(id="use", operation="capabilities.use", data={"bound": ["notes"]})
    _outcome, said, _prompts = await _turn(
        check,
        plans(BIND),
        speaks("It's ready."),
        speaks("I bound notes; nothing is saved yet."),
        results=(bound,),
    )
    assert said == ["I bound notes; nothing is saved yet."]


async def test_a_failed_step_backs_no_claim() -> None:
    check, _events = _check(Laya(FALSE))
    failed = Round(text="", steps=(Step(id="tea", operation="notes.remember", status="error"),))
    assert await check.unbacked(FALSE, [failed]) is True


async def test_a_reply_judged_not_a_claim_goes_out() -> None:
    check, _events = _check(Laya())
    _outcome, said, prompts = await _turn(check, speaks("Tea is a fine choice."))
    assert said == ["Tea is a fine choice."]
    assert prompts.notices == [""]


async def test_an_unsure_yes_lets_the_reply_go() -> None:
    check, _events = _check(Laya(FALSE, probability=0.6))
    assert await check.unbacked(FALSE, []) is False


async def test_in_shadow_the_hold_is_measured_and_the_reply_goes_out() -> None:
    check, events = _check(Laya(FALSE), shadow=True)
    assert await check.unbacked(FALSE, []) is False
    assert "lucy.decision.disagreed" in events


async def test_without_the_decision_nothing_is_asked_and_replies_go_out() -> None:
    laya = Laya(FALSE)
    off, _events = _check(laya, enabled=False)
    assert await off.unbacked(FALSE, []) is False
    assert await ClaimCheck().unbacked(FALSE, []) is False, "a helper has no decisions"
    assert laya.asked == []


def test_the_use_tightens_and_is_on_by_default_behind_the_master_switch() -> None:
    assert CLAIMS in USES
    assert CLAIMS.direction == "tighten"
    policy = TurnPolicy()
    assert policy.decision_claims is True
    assert policy.decisions is False, "the master switch still starts off"
