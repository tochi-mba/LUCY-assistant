"""The contract every other part of the context engine is written against.

These are small types, and the tests are correspondingly small. They are here because the
arithmetic in them decides what a model is told about its own situation, and an off-by-one
in "how full am I" is the kind of thing that is only ever noticed at 200,000 tokens.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lucy_api.context.types import (
    DEFAULT_SHARES,
    Assembled,
    Band,
    Budget,
    BudgetSnapshot,
    LiveState,
    PendingSnapshot,
    Section,
    SessionSnapshot,
    WorkSnapshot,
)


def test_the_shares_spend_the_whole_window_and_no_more() -> None:
    assert sum(DEFAULT_SHARES.values()) == pytest.approx(1.0)


def test_the_reserve_is_the_part_of_the_window_nothing_may_be_written_into() -> None:
    budget = Budget(window=200_000)
    assert budget.usable == 200_000 - budget.allocation(Band.reserve)
    assert budget.usable < budget.window, "a window with no reserve has nowhere to put the reply"


def test_a_band_nobody_gave_a_share_gets_nothing_rather_than_everything() -> None:
    budget = Budget(window=1_000, shares={Band.history: 0.5})
    assert budget.allocation(Band.history) == 500
    assert budget.allocation(Band.tools) == 0


@pytest.mark.parametrize(
    ("used", "window", "expected"),
    [(0, 200_000, 0), (84_000, 200_000, 42), (199_999, 200_000, 99), (200_000, 200_000, 100)],
)
def test_how_full_the_window_is_rounds_down_so_it_never_overstates(used, window, expected) -> None:
    assert BudgetSnapshot(used=used, window=window).percent == expected


def test_a_window_of_zero_reports_nothing_rather_than_dividing_by_it() -> None:
    """A session whose model is not yet known still has to render a state block."""
    assert BudgetSnapshot(used=10, window=0).percent == 0


def test_nothing_is_pending_until_something_is() -> None:
    assert PendingSnapshot().any is False
    assert PendingSnapshot(approvals=("apr_1",)).any is True
    assert PendingSnapshot(elicitations=("eli_1",)).any is True
    assert PendingSnapshot(connections=("music",)).any is True


def test_a_shortened_section_says_so_and_keeps_everything_else_about_itself() -> None:
    original = Section(
        id="tool.1", band=Band.tools, body="the whole thing", tokens=40, priority=7, floor_tokens=5
    )
    shorter = original.with_body("the wh", 2, notice="showing 2 of 40 tokens")
    assert shorter.truncated is True
    assert shorter.notice == "showing 2 of 40 tokens"
    assert (shorter.id, shorter.band, shorter.priority, shorter.floor_tokens) == (
        "tool.1",
        Band.tools,
        7,
        5,
    )
    assert original.truncated is False, "the original is untouched"


def test_a_shortened_section_keeps_its_earlier_notice_when_no_new_one_is_given() -> None:
    original = Section(id="a", band=Band.tools, body="x", tokens=1, notice="already trimmed once")
    assert original.with_body("x", 1).notice == "already trimmed once"


def test_the_prompt_reads_as_its_sections_in_order_and_skips_the_empty_ones() -> None:
    assembled = Assembled(
        sections=(
            Section(id="a", band=Band.system, body="first", tokens=1),
            Section(id="b", band=Band.system, body="", tokens=0),
            Section(id="c", band=Band.history, body="second", tokens=1),
        ),
        by_band={Band.system: 1, Band.history: 1},
        total=2,
    )
    assert assembled.text() == "first\n\nsecond"
    assert [section.id for section in assembled.band(Band.system)] == ["a", "b"]
    assert [section.id for section in assembled.band(Band.tools)] == []


def test_only_the_children_still_working_count_as_running() -> None:
    def child(identifier: str, status: str) -> WorkSnapshot:
        return WorkSnapshot(id=identifier, role="researcher", objective="Look", status=status)

    state = LiveState(
        now=datetime(2026, 9, 17, tzinfo=UTC),
        session=SessionSnapshot(
            id="ses_1", profile="personal", title="t", turn_number=1, permission_mode="ask"
        ),
        budget=BudgetSnapshot(used=1, window=10),
        in_flight=(child("a", "running"), child("b", "finished"), child("c", "queued")),
    )
    assert [agent.id for agent in state.running] == ["a"]
