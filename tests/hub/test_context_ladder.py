"""Reclamation is a projection: the transcript stays, the window names what it gave up."""

from __future__ import annotations

from lucy_api.context.ladder import Limits, reclaim
from lucy_api.context.projection import Item
from lucy_api.context.types import Band, shares_for


def _item(
    seq: int,
    *,
    kind: str = "message",
    role: str = "user",
    body: str = "hello",
    turn: str = "trn_a",
) -> Item:
    return Item(
        id=f"itm_{seq}",
        seq=seq,
        role=role,
        body=body,
        turn_id=turn,
        kind=kind,
        order=seq,
    )


def test_a_window_under_the_warning_line_is_left_alone() -> None:
    items = (
        _item(1),
        _item(2, kind="tool_result", role="tool", body='{"operation": "workspace.read"}'),
    )
    result = reclaim(items, used=1_000, limits=Limits(window=10_000, warn_at_percent=60))

    assert result.items == items
    assert result.notices == ()
    assert result.should_compact is False
    assert result.tools_cleared == 0


def test_crossing_the_warning_line_confesses_the_fill_without_dropping_anything() -> None:
    items = (_item(1),)
    result = reclaim(items, used=6_500, limits=Limits(window=10_000, warn_at_percent=60))

    assert result.items == items
    assert result.tools_cleared == 0
    assert "65%" in result.notices[0]
    assert "10,000" in result.notices[0]
    assert result.should_compact is False


def test_old_tool_results_are_dropped_except_the_kept_tail() -> None:
    items = tuple(
        _item(
            index,
            kind="tool_result",
            role="tool",
            body=f'{{"operation": "workspace.read", "n": {index}}}',
        )
        for index in range(1, 6)
    )
    result = reclaim(
        items,
        used=8_000,
        limits=Limits(window=10_000, compact_at_percent=72, tool_results_kept=3),
    )

    assert [item.seq for item in result.items] == [3, 4, 5]
    assert result.tools_cleared == 2
    assert "cleared 2 of 5 tool results" in result.notices[1]
    assert "kept the last 3" in result.notices[1]
    assert result.should_compact is True


def test_notes_results_are_never_the_ones_reclaimed() -> None:
    memory = _item(1, kind="tool_result", role="tool", body='{"operation": "notes.search"}')
    old = _item(2, kind="tool_result", role="tool", body='{"operation": "workspace.read"}')
    recent = _item(3, kind="tool_result", role="tool", body='{"operation": "workspace.grep"}')
    result = reclaim(
        (memory, old, recent),
        used=9_000,
        limits=Limits(window=10_000, tool_results_kept=1),
    )

    assert memory in result.items
    assert recent in result.items
    assert old not in result.items
    assert result.tools_cleared == 1


def test_thinking_is_cleared_only_after_tool_results_have_been_reclaimed() -> None:
    thought = _item(1, kind="thinking", role="assistant", body="let me consider")
    reason = _item(2, kind="reasoning", role="assistant", body="because")
    tool = _item(3, kind="tool_result", role="tool", body='{"operation": "workspace.read"}')
    spoken = _item(4, kind="message", role="assistant", body="done")
    result = reclaim(
        (thought, reason, tool, spoken),
        used=8_000,
        limits=Limits(window=10_000, tool_results_kept=1),
    )

    assert thought not in result.items
    assert reason not in result.items
    assert tool in result.items
    assert spoken in result.items
    assert result.thinking_cleared == 2
    assert "cleared 2 thinking items" in result.notices[-1]


def test_a_zero_keep_count_drops_every_unprotected_tool_result() -> None:
    items = (
        _item(1, kind="tool_result", role="tool", body='{"op": "workspace.read"}'),
        _item(2, kind="tool_result", role="tool", body='{"operation": "notes.aboutMe"}'),
    )
    result = reclaim(items, used=9_000, limits=Limits(window=10_000, tool_results_kept=0))

    assert [item.seq for item in result.items] == [2]
    assert result.tools_cleared == 1


def test_a_tool_result_that_is_not_json_is_still_reclaimable() -> None:
    blob = _item(1, kind="tool_result", role="tool", body="not-json")
    listed = _item(2, kind="tool_result", role="tool", body="[1]")
    numbered = _item(3, kind="tool_result", role="tool", body='{"operation": 1}')
    result = reclaim(
        (blob, listed, numbered),
        used=9_000,
        limits=Limits(window=10_000, tool_results_kept=0),
    )

    assert result.items == ()
    assert result.tools_cleared == 3


def test_a_full_window_with_no_reclaimable_items_still_asks_for_compaction() -> None:
    spoken = _item(1, kind="message", role="assistant", body="a long answer")
    result = reclaim((spoken,), used=8_000, limits=Limits(window=10_000))

    assert result.items == (spoken,)
    assert result.should_compact is True
    assert result.tools_cleared == 0
    assert result.thinking_cleared == 0


def test_a_window_of_zero_does_not_claim_to_know_how_full_it_is() -> None:
    result = reclaim((_item(1),), used=10, limits=Limits(window=0))
    assert result.notices == ()
    assert result.should_compact is False


def test_a_reserve_outside_the_bounds_is_clamped_to_the_designed_range() -> None:
    assert shares_for(0)[Band.reserve] == 0.05
    assert shares_for(100)[Band.reserve] == 0.40
    from lucy_api.context.types import DEFAULT_SHARES

    designed = shares_for(13)
    assert designed[Band.reserve] == DEFAULT_SHARES[Band.reserve]
    assert abs(sum(designed.values()) - 1.0) < 1e-9
    wider = shares_for(20)
    assert wider[Band.reserve] == 0.20
    assert wider[Band.tools] < designed[Band.tools]
    assert abs(sum(wider.values()) - 1.0) < 1e-9
