"""`Decisions` never raises, never acts while shadowed, and never invents a topic.

The test that matters most is `test_a_decider_that_misbehaves_is_indistinguishable_from_none`:
whatever a decider does -- time out, raise, return nonsense -- a caller sees exactly what it
sees with no decider at all. Everything else in this file is a consequence of that.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from weftai.decisions import Answer, Answers, AnyQuestion, NullDecider, noul

from lucy_api.decide import DISAGREED, MADE, Decisions
from lucy_api.decide.memory import (
    ABSTAIN,
    MAX_TOPICS,
    decided_key,
    topic_batch,
    topic_question,
)
from lucy_api.decide.types import TOPIC, USES, Skip


class Recorder:
    """Collects emitted events, so a test can read what a person would see."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append((name, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    def reasons(self) -> list[str]:
        return [fields.get("reason", "") for _, fields in self.events]


class Scripted:
    """Answers with whatever it was handed. The golden-transcript decider."""

    def __init__(self, answers: Answers, *, delay: float = 0.0) -> None:
        self.answers = answers
        self.delay = delay
        self.calls = 0

    async def decide(self, state: str, questions: list[AnyQuestion]) -> Answers:
        _ = state, questions
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.answers


class Exploding:
    async def decide(self, state: str, questions: list[AnyQuestion]) -> Answers:
        _ = state, questions
        raise RuntimeError("the provider returned the response body in the message")


class Nonsense:
    async def decide(self, state: str, questions: list[AnyQuestion]) -> Answers:
        _ = state, questions
        return "not answers"  # type: ignore[return-value]


def answers(*pairs: tuple[str, str, float]) -> Answers:
    return Answers([Answer(question, "choice", value, p) for question, value, p in pairs])


def live(decider: object, emit: Recorder | None = None) -> Decisions:
    """A `Decisions` with the topic use enabled and shadow off -- answers may be acted on."""
    return Decisions(
        decider,  # type: ignore[arg-type]
        enabled=[TOPIC.id],
        shadow=False,
        emit=emit,
    )


# --- the contract -------------------------------------------------------------------------


async def test_the_default_answers_nothing() -> None:
    """Off is the default at every level, so a bare `Decisions` decides nothing."""
    decisions = Decisions()
    assert not decisions.enabled
    assert decisions.shadow is True
    assert not decisions.live(TOPIC)
    assert (await decisions.ask(TOPIC, "state", [])).empty


@pytest.mark.parametrize(
    "decider",
    [Exploding(), Nonsense(), Scripted(Answers()), NullDecider()],
    ids=["raises", "returns nonsense", "answers nothing", "is the null decider"],
)
async def test_a_decider_that_misbehaves_is_indistinguishable_from_none(decider: object) -> None:
    """The whole contract. A caller cannot tell these apart, so it never has to."""
    result = await live(decider).ask(TOPIC, "state", [noul("a", "Is it?")])
    assert result.empty
    assert decided_key(result, ["home", "work"]) == ""


async def test_a_timeout_falls_open_and_is_named() -> None:
    emit = Recorder()
    decisions = Decisions(
        Scripted(answers(("topic", "home", 0.9)), delay=0.2),
        enabled=[TOPIC.id],
        shadow=False,
        timeout_ms=10,
        emit=emit,
    )
    assert (await decisions.ask(TOPIC, "state", [])).empty
    assert emit.reasons() == [Skip.TIMEOUT]


async def test_an_exception_is_named_without_its_message() -> None:
    """A client often puts the response body in the message; it has no business in an event."""
    emit = Recorder()
    await live(Exploding(), emit).ask(TOPIC, "state", [])
    assert emit.reasons() == [Skip.MALFORMED]
    assert "response body" not in str(emit.events)


# --- the three gates ----------------------------------------------------------------------


async def test_a_use_that_is_not_enabled_is_not_asked() -> None:
    emit = Recorder()
    decider = Scripted(answers(("topic", "home", 0.9)))
    decisions = Decisions(decider, enabled=[], shadow=False, emit=emit)
    assert (await decisions.ask(TOPIC, "state", [])).empty
    assert decider.calls == 0
    assert emit.reasons() == [Skip.OFF]


async def test_shadow_mode_asks_and_measures_but_is_not_live() -> None:
    """The point of shadow mode: the call happens, the answer is not acted on."""
    emit = Recorder()
    decider = Scripted(answers(("topic", "home", 0.9)))
    decisions = Decisions(decider, enabled=[TOPIC.id], shadow=True, emit=emit)
    result = await decisions.ask(TOPIC, "state", [])
    assert decider.calls == 1
    assert not result.empty
    assert not decisions.live(TOPIC)
    assert emit.names() == [MADE]
    assert emit.events[0][1]["shadow"] is True


async def test_all_three_gates_must_be_open_for_a_use_to_be_live() -> None:
    assert not Decisions(enabled=[], shadow=False).live(TOPIC)
    assert not Decisions(enabled=[TOPIC.id], shadow=True).live(TOPIC)
    assert Decisions(enabled=[TOPIC.id], shadow=False).live(TOPIC)


async def test_no_decider_means_nothing_is_asked() -> None:
    emit = Recorder()
    decisions = Decisions(NullDecider(), enabled=[TOPIC.id], shadow=False, emit=emit)
    assert (await decisions.ask(TOPIC, "state", [])).empty
    assert emit.reasons() == [Skip.NO_DECIDER]


# --- budget -------------------------------------------------------------------------------


async def test_the_turn_budget_falls_open_for_the_rest_of_the_turn() -> None:
    emit = Recorder()
    decider = Scripted(answers(("topic", "home", 0.9)))
    decisions = Decisions(decider, enabled=[TOPIC.id], shadow=False, max_per_turn=1, emit=emit)
    assert not (await decisions.ask(TOPIC, "state", [])).empty
    assert (await decisions.ask(TOPIC, "state", [])).empty
    assert decider.calls == 1
    assert decisions.spent == 1
    assert emit.reasons()[-1] == Skip.TURN_BUDGET


async def test_a_budget_of_zero_means_unlimited() -> None:
    decider = Scripted(answers(("topic", "home", 0.9)))
    decisions = Decisions(decider, enabled=[TOPIC.id], shadow=False, max_per_turn=0)
    for _ in range(3):
        await decisions.ask(TOPIC, "state", [])
    assert decider.calls == 3


# --- events -------------------------------------------------------------------------------


async def test_a_made_event_carries_the_answers_and_never_the_state() -> None:
    emit = Recorder()
    await live(Scripted(answers(("topic", "home", 0.87))), emit).ask(
        TOPIC, "a note about the wifi password", []
    )
    name, fields = emit.events[0]
    assert name == MADE
    assert fields["use"] == "topic"
    assert fields["answers"][0] == {
        "id": "topic",
        "kind": "choice",
        "value": "home",
        "probability": 0.87,
    }
    assert "wifi" not in str(fields)


async def test_disagreement_is_emitted_only_when_the_answers_differ() -> None:
    emit = Recorder()
    decisions = live(Scripted(Answers()), emit)
    await decisions.disagreed(TOPIC, decided="home", fallback="home")
    assert emit.events == []
    await decisions.disagreed(TOPIC, decided="home", fallback="work")
    assert emit.names() == [DISAGREED]
    assert emit.events[0][1] == {
        "use": "topic",
        "shadow": False,
        "decided": "home",
        "fallback": "work",
    }


async def test_the_default_emitter_is_silent_rather_than_absent() -> None:
    """Nothing is wired in a unit test, and that must not be a branch at every call site."""
    assert (
        await Decisions(Exploding(), enabled=[TOPIC.id], shadow=False).ask(  # type: ignore[arg-type]
            TOPIC, "state", []
        )
    ).empty


# --- the topic use ------------------------------------------------------------------------


def test_the_question_carries_no_content_only_the_options() -> None:
    question = topic_question("a note about the wifi password", ["home", "work"])
    assert question.options == ("home", "work")
    assert question.abstain == ABSTAIN
    assert "wifi" not in question.prompt


def test_a_confident_choice_is_taken() -> None:
    assert decided_key(answers(("topic", "home", 0.9)), ["home", "work"]) == "home"


def test_an_unconfident_choice_leaves_it_to_overlap() -> None:
    assert decided_key(answers(("topic", "home", 0.3)), ["home", "work"]) == ""


def test_abstaining_leaves_it_to_overlap() -> None:
    """Which is where a genuinely new subject belongs, and the overlap pass agrees."""
    assert decided_key(answers(("topic", ABSTAIN, 0.99)), ["home", "work"]) == ""


def test_a_key_outside_the_option_set_is_refused() -> None:
    """A key the model invented is not a key, however confident it was."""
    assert decided_key(answers(("topic", "invented", 0.99)), ["home", "work"]) == ""


def test_no_answer_leaves_it_to_overlap() -> None:
    assert decided_key(Answers(), ["home", "work"]) == ""


@pytest.mark.parametrize("keys", [[], ["only"], [f"t{i}" for i in range(MAX_TOPICS + 1)]])
def test_there_is_nothing_to_ask_below_two_topics_or_above_the_cap(keys: list[str]) -> None:
    assert topic_batch("a note", keys) is None


def test_a_workable_topic_set_produces_one_question() -> None:
    batch = topic_batch("a note", ["home", "work"])
    assert batch is not None
    assert [q.id for q in batch.questions] == ["topic"]


# --- the declarations ---------------------------------------------------------------------


def test_every_declared_use_tightens_or_says_why_not() -> None:
    """The invariant, read back from the declarations rather than from the prose."""
    for use in USES:
        assert use.direction in ("tighten", "advisory")
        assert use.fallback, f"{use.id} has no deterministic path behind it"
        assert use.setting.startswith("decision_")


def test_the_topic_use_tightens() -> None:
    assert TOPIC.tightens
    assert TOPIC.setting == "decision_topic"
