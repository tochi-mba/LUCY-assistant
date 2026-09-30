"""A turn that expects the hub to refuse the message: an outage held as a conversation.

The bug, named: Rung 6 of the live ladder stops settings mid-conversation and checks that the
next message is refused -- "Settings could not be reached, and this turn's safety limits
cannot be guessed" -- rather than run with limits the hub had to guess. The hub refuses it
at the door with a 503, and the harness took any refusal as the run breaking, so the one
answer the rung was written to see could only ever be reported as an error.

Held here through the real runner and `HttpHub` against `FakeLucy`, which answers a message
it is told to refuse with a problem, exactly as the hub's error handler writes one.
"""

from __future__ import annotations

import pytest
from eval_fakes import Clock, FakeLucy

from lucy_api.evals.conversation import Pace
from lucy_api.evals.loader import ScenarioError, parse_scenario
from lucy_api.evals.markdown import _turn
from lucy_api.evals.report import summarize
from lucy_api.evals.results import ERROR, FAILED, PASSED, ScenarioRecord
from lucy_api.evals.runner import Plan, Runner
from lucy_api.evals.scenario import Scenario

MODEL = "clyde:haiku"
UNREACHABLE = (
    "Settings could not be reached, and this turn's safety limits cannot be guessed. "
    "Try again in a moment."
)
REFUSED_AS = "POST /v1/sessions/ses_1/inputs answered 503: " + UNREACHABLE

OUTAGE = """\
summary = "With settings down, a message is refused; with them back, the next one is taken."

[[turns]]
say = "Remember that I like green tea."

[turns.expect]
refused = "settings-unavailable"

[[turns]]
say = "Are you back?"
"""


def held(fake: FakeLucy, text: str) -> ScenarioRecord:
    clock = Clock()
    scenario = parse_scenario(text.encode(), name="outage", suite="tests", path="tests/o.toml")
    runner = Runner(fake.hub(), pace=Pace(clock=clock, sleep=clock.sleep))
    records: list[ScenarioRecord] = []
    runner.run(Plan(scenarios=(scenario,), models=(MODEL,)), records.append)
    return records[0]


def test_a_message_refused_as_the_turn_expects_passes_and_the_conversation_goes_on() -> None:
    fake = FakeLucy()
    fake.refusals["Remember that I like green tea."] = (503, "settings-unavailable", UNREACHABLE)

    record = held(fake, OUTAGE)

    assert record.outcome == PASSED
    refused, taken = record.turns
    assert (refused.status, refused.turn_id, refused.reply) == ("refused", "", "")
    assert refused.refused == REFUSED_AS
    checks = {check.name: (check.passed, check.detail) for check in refused.checks}
    assert checks["turn 1: status is refused"] == (True, "ended refused")
    assert checks["turn 1: the hub refused the message as settings-unavailable"] == (
        True,
        REFUSED_AS,
    )
    assert checks["turn 1: came to rest before the timeout"] == (True, "took 0.0s of 300.0s")
    assert taken.status == "completed"
    assert list(fake.turns) == ["trn_1"]


def test_a_refusal_for_another_reason_fails_the_turn_with_the_hub_s_sentence() -> None:
    fake = FakeLucy()
    fake.refusals["Remember that I like green tea."] = (503, "model-unavailable", "No model.")

    record = held(fake, OUTAGE)

    assert record.outcome == FAILED
    [failing] = [check for check in record.checks if not check.passed]
    assert failing.name == "turn 1: the hub refused the message as settings-unavailable"
    assert failing.detail == "POST /v1/sessions/ses_1/inputs answered 503: No model."


def test_a_message_the_hub_takes_when_a_refusal_was_expected_fails_and_the_turn_runs() -> None:
    fake = FakeLucy()

    record = held(fake, OUTAGE)

    assert record.outcome == FAILED
    first = record.turns[0]
    assert first.status == "completed"
    failing = {check.name: check.detail for check in first.checks if not check.passed}
    assert failing == {
        "turn 1: status is refused": "ended completed (success)",
        "turn 1: the hub refused the message as settings-unavailable": (
            "the hub took it, and the turn ended completed"
        ),
    }


def test_a_refusal_no_turn_expected_still_stops_the_conversation() -> None:
    fake = FakeLucy()
    fake.refusals["Are you back?"] = (503, "settings-unavailable", UNREACHABLE)
    text = '[[turns]]\nsay = "Are you back?"\n'

    record = held(fake, 'summary = "A refusal nobody expected."\n' + text)

    assert record.outcome == ERROR
    assert UNREACHABLE in record.reason


def test_a_refusal_the_hub_names_no_problem_for_still_stops_the_conversation() -> None:
    """An answer with no problem ``type`` is not the hub's refusal; it is something broken."""
    fake = FakeLucy()
    fake.fail[("POST", "/v1/sessions/ses_1/inputs")] = 503

    record = held(fake, OUTAGE)

    assert record.outcome == ERROR
    assert record.turns == ()


# --- how the file says it --------------------------------------------------------------------


def _refusal(expect: str) -> str:
    return f'summary = "s"\n[[turns]]\nsay = "Hi"\n[turns.expect]\n{expect}\n'


def _parsed(expect: str) -> Scenario:
    return parse_scenario(_refusal(expect).encode(), name="s", suite="tests", path="tests/s.toml")


def test_expecting_a_refusal_makes_refused_the_status_the_turn_rests_in() -> None:
    scenario = _parsed('refused = "settings-unavailable"')
    [turn] = scenario.turns
    assert (turn.expect.refused, turn.expect.status) == ("settings-unavailable", "refused")
    assert turn.expect.reply_nonempty is False


def test_a_refusal_is_named_the_way_the_hub_names_a_problem() -> None:
    with pytest.raises(ScenarioError, match=r"expect\.refused: must name the problem the hub"):
        _parsed('refused = "Settings Unavailable"')


def test_a_refused_turn_cannot_be_told_to_rest_anywhere_else() -> None:
    with pytest.raises(ScenarioError, match=r'expect\.status: must be "refused" when the turn'):
        _parsed('refused = "settings-unavailable"\nstatus = "completed"')
    with pytest.raises(ScenarioError, match=r'expect\.status: "refused" needs `refused`'):
        _parsed('status = "refused"')


# --- how the reports read it -----------------------------------------------------------------


def test_a_refused_message_is_not_a_turn_in_the_summary_and_says_why_in_the_markdown() -> None:
    fake = FakeLucy()
    fake.refusals["Remember that I like green tea."] = (503, "model-unavailable", "No model.")
    record = held(fake, OUTAGE)
    runs = [record.to_dict()]

    [row] = summarize(runs, [MODEL]).values()
    assert row["turns"] == 1

    lines = "\n".join(_turn(runs[0]["turns"][0]))
    assert "#### Turn 1: refused in" in lines
    assert "The hub refused it:" in lines
    assert "No model." in lines
    assert "Lucy:" not in lines
