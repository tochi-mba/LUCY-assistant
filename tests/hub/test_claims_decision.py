"""A decision catches a completion claim the phrase list does not know, and only ever adds a hold.

The phrase list behind `lucy_api.turn.claims` is English and names the shapes seen. It cannot
know "Consider it remembered", "Tea's in your notes now", or the same thing in any other
language. When `decision_claims` is enabled, a reply the phrase list passed is asked about.
"""

from __future__ import annotations

from typing import Any

from test_turn_loop import Prompts, Transcript
from weftai.decisions import Answer, Answers

from lucy_api.decide import Decisions
from lucy_api.decide.types import CLAIMS, USES
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.settings.policy import TurnPolicy
from lucy_api.turn.claims import QUESTION, UNBACKED, ClaimCheck
from lucy_api.turn.loop import Round, Step, Turn, run_turn

UNLISTED = "Consider it remembered: tea, always."
"""A claim, in words the phrase list does not know."""


class Laya:
    """A decider that answers yes or no to every question, at one probability of yes."""

    def __init__(self, *, yes: bool, probability: float = 0.97) -> None:
        self.yes = yes
        self.probability = probability
        self.asked: list[tuple[str, Any]] = []

    async def decide(self, state: str, questions: Any) -> Answers:
        self.asked.append((state, questions))
        return Answers([Answer(q.id, "noul", self.yes, self.probability) for q in questions])


def _decide(
    laya: Laya, *, shadow: bool = False, enabled: bool = True
) -> tuple[Decisions, list[str]]:
    events: list[str] = []

    async def emit(name: str, _fields: dict[str, Any]) -> None:
        events.append(name)

    decisions = Decisions(laya, enabled=[CLAIMS.id] if enabled else [], shadow=shadow, emit=emit)
    return decisions, events


async def test_a_claim_in_words_the_list_does_not_know_is_caught_by_the_decision() -> None:
    """The limit, named: the phrase list lets this through."""
    laya = Laya(yes=True)
    decisions, events = _decide(laya)

    assert await ClaimCheck(decisions).unbacked(UNLISTED, []) is True
    [(state, [question])] = laya.asked
    assert UNLISTED in state
    assert question.prompt == QUESTION
    assert "lucy.decision.disagreed" in events


async def test_a_held_claim_never_reaches_the_person_in_a_real_turn() -> None:
    decisions, _events = _decide(Laya(yes=True))
    transcript, prompts = Transcript(), Prompts()
    await run_turn(
        Turn(
            provider=ScriptedProvider([speaks(UNLISTED), speaks("I have not saved anything yet.")]),
            assemble=prompts.assemble,
            append=transcript.append,
            claims=ClaimCheck(decisions),
        )
    )
    said = [content for kind, _role, content in transcript.items if kind == "message"]
    assert said == ["I have not saved anything yet."]
    assert UNBACKED in prompts.notices[1]


async def test_in_shadow_the_disagreement_is_measured_and_the_reply_goes_out() -> None:
    decisions, events = _decide(Laya(yes=True), shadow=True)
    assert await ClaimCheck(decisions).unbacked(UNLISTED, []) is False
    assert "lucy.decision.disagreed" in events


async def test_a_confident_no_or_an_unsure_yes_lets_the_reply_go() -> None:
    for laya in (Laya(yes=False, probability=0.97), Laya(yes=True, probability=0.6)):
        decisions, _events = _decide(laya)
        assert await ClaimCheck(decisions).unbacked(UNLISTED, []) is False


async def test_the_decision_never_overrules_the_phrase_list() -> None:
    """`tighten`: a claim the list catches is held back without asking, whatever Laya thinks."""
    laya = Laya(yes=False)
    decisions, _events = _decide(laya)
    assert await ClaimCheck(decisions).unbacked("I've saved that.", []) is True
    assert laya.asked == []


async def test_nothing_is_asked_after_a_step_did_real_work_or_when_the_use_is_off() -> None:
    worked = [Round(text="", steps=(Step(id="t", operation="notes.remember", status="ok"),))]
    on_laya, off_laya = Laya(yes=True), Laya(yes=True)
    on, _ = _decide(on_laya)
    off, _ = _decide(off_laya, enabled=False)

    assert await ClaimCheck(on).unbacked(UNLISTED, worked) is False
    assert await ClaimCheck(off).unbacked(UNLISTED, []) is False
    assert await ClaimCheck().unbacked(UNLISTED, []) is False
    assert on_laya.asked == off_laya.asked == []


def test_the_use_tightens_and_is_on_by_default_behind_the_master_switch() -> None:
    assert CLAIMS in USES
    assert CLAIMS.direction == "tighten"
    policy = TurnPolicy()
    assert policy.decision_claims is True
    assert policy.decisions is False, "the master switch still starts off"
