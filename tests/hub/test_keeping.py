"""A turn about to end with something worth keeping left unkept is held back once.

Lucy is told to keep what is worth keeping without being asked. The weakest model still lets
one go by now and then, most often when the message was mostly about something else, and the
person says it again next week. Whether a message held something worth keeping is a question
about what the person meant, so a model answers it: the `keeping` Laya decision. These tests
stand in for Laya with a decider that answers as told.
"""

from __future__ import annotations

from typing import Any

from test_claims import Laya
from test_turn_loop import Prompts, Transcript, executor, ok_result

from lucy_api.decide import Decisions
from lucy_api.decide.types import KEEPING, USES
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.context import PackContext, SilentTokens
from lucy_api.packs.http import NullHttp
from lucy_api.settings.policy import TurnPolicy
from lucy_api.turn.keeping import QUESTION, UNKEPT, Keeping
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.stop import Termination
from lucy_api.turn.supervisor import _may_keep

REQUEST = "btw im a backend eng, mostly python. anyway can u check the weather"
ANSWER = "It's 18 and clear."
KEEP = {
    "steps": [
        {
            "id": "role",
            "op": "notes.setFact",
            "input": {"title": "Role", "body": "Backend engineer, mostly Python."},
            "note": "Keep what they do.",
        }
    ]
}


def _keeping(
    laya: Laya, *, shadow: bool = False, enabled: bool = True, able: bool = True
) -> tuple[Keeping, list[str]]:
    events: list[str] = []

    async def emit(name: str, _fields: dict[str, Any]) -> None:
        events.append(name)

    decisions = Decisions(laya, enabled=[KEEPING.id] if enabled else [], shadow=shadow, emit=emit)
    decisions.request_text = REQUEST
    return Keeping(decisions, able=able), events


async def _turn(
    keeping: Keeping, *script: Any, results: tuple[dict[str, Any], ...] = ()
) -> tuple[Any, list[str], Prompts]:
    transcript, prompts = Transcript(), Prompts()
    outcome = await run_turn(
        Turn(
            provider=ScriptedProvider(list(script)),
            assemble=prompts.assemble,
            append=transcript.append,
            execute=executor(*results) if results else None,
            keeping=keeping,
        )
    )
    said = [content for kind, _role, content in transcript.items if kind == "message"]
    return outcome, said, prompts


async def test_a_reply_that_leaves_a_fact_unkept_is_held_back_and_the_fact_kept() -> None:
    """The bug, named: the person said what they do, got the weather, and was not remembered."""
    laya = Laya(REQUEST)
    keeping, events = _keeping(laya)
    kept = ok_result(id="role", operation="notes.setFact", data={"id": "mem_1"})

    outcome, said, prompts = await _turn(
        keeping,
        speaks(ANSWER),
        plans(KEEP),
        speaks("Noted that you're a backend engineer. It's 18 and clear."),
        results=(kept,),
    )

    assert outcome.termination is Termination.success
    assert said == ["Noted that you're a backend engineer. It's 18 and clear."]
    assert UNKEPT in prompts.notices[1]
    [(state, [question])] = laya.asked
    assert REQUEST in state, "the judgment sees what was said, typos and all"
    assert question.prompt == QUESTION
    assert "lucy.decision.disagreed" in events


async def test_the_judgment_sees_which_steps_ran_so_a_keep_already_made_counts() -> None:
    laya = Laya(REQUEST)
    keeping, _events = _keeping(laya)
    kept = ok_result(id="role", operation="notes.setFact", data={"id": "mem_1"})
    await _turn(keeping, plans(KEEP), speaks(ANSWER), speaks(ANSWER), results=(kept,))

    [(state, _questions)] = laya.asked
    assert '"operation": "notes.setFact"' in state
    assert '"for": "Keep what they do."' in state, "the plan's sentence, as the transcript has it"


async def test_a_reply_is_held_back_once_and_the_second_goes_out() -> None:
    keeping, _events = _keeping(Laya(REQUEST))
    outcome, said, _prompts = await _turn(keeping, speaks(ANSWER), speaks(ANSWER))
    assert outcome.termination is Termination.success
    assert said == [ANSWER]


async def test_a_reply_judged_to_have_missed_nothing_goes_out() -> None:
    keeping, _events = _keeping(Laya())
    _outcome, said, prompts = await _turn(keeping, speaks(ANSWER))
    assert said == [ANSWER]
    assert prompts.notices == [""]


async def test_an_unsure_yes_lets_the_reply_go() -> None:
    keeping, _events = _keeping(Laya(REQUEST, probability=0.6))
    assert await keeping.unkept([]) is False


async def test_in_shadow_the_hold_is_measured_and_the_reply_goes_out() -> None:
    keeping, events = _keeping(Laya(REQUEST), shadow=True)
    assert await keeping.unkept([]) is False
    assert "lucy.decision.disagreed" in events


async def test_a_conversation_that_may_keep_nothing_is_not_asked() -> None:
    laya = Laya(REQUEST)
    keeping, _events = _keeping(laya, able=False)
    assert await keeping.unkept([]) is False
    assert laya.asked == []


async def test_without_the_decision_or_a_request_nothing_is_asked() -> None:
    laya = Laya(REQUEST)
    off, _events = _keeping(laya, enabled=False)
    assert await off.unkept([]) is False
    assert await Keeping().unkept([]) is False, "a helper has no decisions"
    silent, _events = _keeping(laya)
    silent.decide.request_text = ""
    assert await silent.unkept([]) is False
    assert laya.asked == []


def test_a_conversation_may_keep_unless_incognito_off_or_without_notes() -> None:
    def context(**overrides: Any) -> PackContext:
        fields: dict[str, Any] = {
            "account_id": "acct",
            "profile": "personal",
            "session_id": "ses",
            "http": NullHttp(),
            "tokens": SilentTokens(),
            "policy": TurnPolicy(),
        }
        return PackContext(**{**fields, **overrides})

    assert _may_keep(context(), ("help", "notes")) is True
    assert _may_keep(context(incognito=True), ("help", "notes")) is False
    assert _may_keep(context(policy=TurnPolicy(memory_write_policy="never")), ("notes",)) is False
    assert _may_keep(context(), ("help",)) is False


def test_the_use_is_advisory_and_on_by_default_behind_the_master_switch() -> None:
    assert KEEPING in USES
    assert KEEPING.direction == "advisory"
    policy = TurnPolicy()
    assert policy.decision_keeping is True
    assert policy.decisions is False, "the master switch still starts off"
