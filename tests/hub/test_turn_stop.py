"""When a turn stops, and whether a client may offer to try again.

The distinction being pinned down is the one that shows up in a person's face: offering a
retry on a refusal teaches them that a refusal is a rate limit, and refusing to offer one
after an iteration cap makes a resumable turn look like a dead end.
"""

from __future__ import annotations

import pytest

from lucy_api.turn.stop import (
    RESUMABLE,
    WAITING,
    WRAP_UP,
    Budget,
    Spent,
    Termination,
    Verdict,
    should_stop,
    warning_for,
)


def test_a_fresh_turn_carries_on() -> None:
    assert should_stop(Budget(), Spent()).stop is False


def test_a_turn_that_keeps_going_round_is_stopped_and_can_be_resumed() -> None:
    verdict = should_stop(Budget(max_iterations=12), Spent(iterations=12))

    assert verdict.stop is True
    assert verdict.termination is Termination.max_iterations
    assert verdict.resumable is True
    assert "12 model rounds" in verdict.detail, "it says how far it got, not just that it stopped"


def test_a_turn_that_spends_its_tokens_says_how_many() -> None:
    verdict = should_stop(Budget(max_tokens=1_000), Spent(tokens=1_200))

    assert verdict.termination is Termination.max_budget
    assert "1,200 tokens" in verdict.detail


def test_a_turn_that_runs_too_long_is_stopped_first_of_all() -> None:
    """Time is checked before money and before rounds.

    A turn that has been going for two minutes is one somebody is watching, and the count
    of rounds is a backstop rather than the thing anybody cares about.
    """
    verdict = should_stop(
        Budget(max_seconds=30, max_tokens=10, max_iterations=1),
        Spent(seconds=31, tokens=999, iterations=99),
    )
    assert "31s" in verdict.detail


def test_too_many_tool_calls_stops_a_turn_that_is_not_going_round_in_rounds() -> None:
    """A single round can still run sixty steps. The iteration count would never notice."""
    verdict = should_stop(Budget(max_tool_calls=10), Spent(tool_calls=10))

    assert verdict.termination is Termination.max_iterations
    assert "10 tool calls" in verdict.detail


def test_a_budget_of_zero_tokens_means_no_limit_rather_than_no_turn() -> None:
    assert Budget(max_tokens=0).unlimited_tokens is True
    assert should_stop(Budget(max_tokens=0), Spent(tokens=10_000_000)).stop is False


def test_a_time_limit_of_zero_means_no_limit() -> None:
    assert should_stop(Budget(max_seconds=0), Spent(seconds=10_000)).stop is False


@pytest.mark.parametrize(
    ("termination", "resumable"),
    [
        (Termination.max_iterations, True),
        (Termination.max_budget, True),
        (Termination.failed, True),
        (Termination.refused, False),
        (Termination.success, False),
        (Termination.cancelled, False),
    ],
)
def test_only_the_terminations_worth_retrying_are_resumable(
    termination: Termination, resumable: bool
) -> None:
    assert Verdict(stop=True, termination=termination).resumable is resumable
    assert (termination in RESUMABLE) is resumable


def test_waiting_is_neither_finished_nor_failed() -> None:
    """A turn waiting on an approval has not gone wrong, and must not be shown as an error."""
    assert Termination.input_required in WAITING
    assert Termination.auth_required in WAITING
    assert not (WAITING & RESUMABLE), "nothing waiting is retried; something else must move"


def test_needing_a_credential_is_told_apart_from_needing_a_decision() -> None:
    """One routes to a connect flow and the other to a prompt. Conflating them sends
    somebody to the wrong place at the moment they are already stuck."""
    assert Termination.auth_required is not Termination.input_required


# --------------------------------------------------------------------------------------
# Warning before the wall
# --------------------------------------------------------------------------------------


def test_nothing_is_said_while_there_is_plenty_left() -> None:
    assert warning_for(Budget(max_iterations=10), Spent(iterations=2)) == ""


def test_the_model_is_warned_with_a_round_still_in_hand() -> None:
    """So it can say where it got to. A model that hits the wall says nothing."""
    notice = warning_for(Budget(max_iterations=10), Spent(iterations=8))

    assert "2 of 10 model rounds left" in notice
    assert notice.endswith(WRAP_UP)
    assert "write down" not in notice, "the person needs to know, not a note"


def test_a_token_budget_warns_too() -> None:
    notice = warning_for(Budget(max_tokens=1_000), Spent(tokens=900))
    assert "100 tokens left" in notice


def test_an_unlimited_token_budget_never_warns_about_tokens() -> None:
    assert warning_for(Budget(max_tokens=0), Spent(tokens=10_000_000)) == ""


def test_the_threshold_can_be_moved() -> None:
    early = warning_for(Budget(max_iterations=10), Spent(iterations=5), at=0.5)
    assert "5 of 10 model rounds left" in early


def test_every_limit_that_ends_a_turn_is_warned_about_first() -> None:
    """The bug, named: `should_stop` ends a turn at its tool-call and time limits, and neither
    was ever warned about, so the turn ended with the work unreported."""
    calls = warning_for(Budget(max_tool_calls=10), Spent(tool_calls=9))
    assert calls == f"1 of 10 tool calls left in this turn: {WRAP_UP}"
    clock = warning_for(Budget(max_seconds=100.0), Spent(seconds=90.5))
    assert clock == f"about 9s left in this turn: {WRAP_UP}"
    late = warning_for(Budget(max_seconds=100.0), Spent(seconds=130.0))
    assert late.startswith("about 0s left")
    tokens = warning_for(Budget(max_tokens=1_000), Spent(tokens=900))
    assert tokens == f"100 tokens left in this conversation's budget: {WRAP_UP}"
