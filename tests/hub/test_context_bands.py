"""The band allocator, pinned behaviour by behaviour.

The properties at the bottom are the ones worth stating about *any* input rather than about
a chosen one: no band overspends, and nothing vanishes unaccounted for. They are plain
loops over a seeded `random.Random` on purpose -- the repository does not take a dependency
on hypothesis for two properties, and a fixed seed is a failure anyone can reproduce by
reading the parameter.
"""

from __future__ import annotations

import random
import re

import pytest

from lucy_api.context.bands import (
    HEAD_TAIL_MINIMUM_CHARS,
    WRITABLE_BANDS,
    allocate,
)
from lucy_api.context.tokens import CHARS_PER_TOKEN, Estimate
from lucy_api.context.types import Band, Budget, Section

COUNTER = Estimate()
MARKER = "[... showing "


def filler(tokens: int) -> str:
    """A body that costs exactly `tokens` under the estimate, and has no line breaks."""
    return "x" * (tokens * CHARS_PER_TOKEN)


def rows(count: int, width: int = 60) -> str:
    """A body of `count` numbered lines, the shape a log or a diff arrives in."""
    return "\n".join(f"line {index:04d} " + "x" * width for index in range(count))


def section(
    identifier: str,
    body: str,
    *,
    band: Band = Band.tools,
    priority: int = 50,
    floor: int = 0,
) -> Section:
    return Section(
        id=identifier,
        band=band,
        body=body,
        tokens=COUNTER.count(body),
        priority=priority,
        floor_tokens=floor,
    )


def one_band(band: Band, allocation: int) -> Budget:
    """A budget that gives one band everything, so a test can name an exact ceiling."""
    return Budget(window=allocation, shares={band: 1.0})


class Words:
    """A counter that disagrees with the estimate: one token per whitespace-separated word."""

    def count(self, text: str) -> int:
        return len(text.split())


class Expensive:
    """A counter for which nothing at all is affordable, confession included."""

    def count(self, text: str) -> int:
        return 1_000 if text else 0


# --------------------------------------------------------------------------------------
# The property that matters most: bands do not spend each other's money.
# --------------------------------------------------------------------------------------


def test_a_band_never_spends_another_bands_budget() -> None:
    budget = Budget(window=1_000)
    pinned = section("who-the-person-is", filler(25), band=Band.pinned)
    huge = section("a-very-large-tool-result", filler(2_000), band=Band.tools)

    assembled = allocate([pinned, huge], budget, COUNTER)

    kept = assembled.band(Band.pinned)[0]
    assert kept.body == pinned.body
    assert kept.truncated is False
    assert assembled.by_band[Band.pinned] == 25
    assert assembled.by_band[Band.tools] <= budget.allocation(Band.tools)


def test_a_bands_outcome_does_not_change_when_another_band_overflows() -> None:
    budget = Budget(window=1_000)
    tools = [
        section("tool-a", filler(400), band=Band.tools),
        section("tool-b", filler(400), band=Band.tools, priority=90),
    ]
    flood = section("the-whole-conversation", filler(9_000), band=Band.history)

    alone = allocate(tools, budget, COUNTER)
    crowded = allocate([*tools, flood], budget, COUNTER)

    assert crowded.band(Band.tools) == alone.band(Band.tools)
    assert crowded.by_band[Band.tools] == alone.by_band[Band.tools]


def test_sections_are_given_up_in_descending_priority_order() -> None:
    budget = one_band(Band.tools, 500)
    sections = [
        section("keep-me", filler(200), priority=0),
        section("middling", filler(200), priority=50),
        section("give-me-up-first", filler(200), priority=90),
    ]

    assembled = allocate(sections, budget, COUNTER)

    truncated = {kept.id: kept.truncated for kept in assembled.sections}
    assert truncated == {"keep-me": False, "middling": False, "give-me-up-first": True}


def test_a_tie_in_priority_is_broken_by_section_id_so_two_runs_agree() -> None:
    budget = one_band(Band.tools, 300)
    sections = [
        section("zebra", filler(200)),
        section("aardvark", filler(200)),
    ]

    first = allocate(sections, budget, COUNTER)
    again = allocate(list(reversed(sections)), budget, COUNTER)

    assert [kept.truncated for kept in first.sections] == [False, True]
    assert {kept.id for kept in first.sections if kept.truncated} == {"aardvark"}
    assert {kept.id for kept in again.sections if kept.truncated} == {"aardvark"}


# --------------------------------------------------------------------------------------
# Shortened, or dropped whole: the floor decides.
# --------------------------------------------------------------------------------------


def test_a_section_is_shortened_while_what_is_left_stays_at_or_above_its_floor() -> None:
    budget = one_band(Band.tools, 500)

    assembled = allocate([section("log", filler(1_000), floor=100)], budget, COUNTER)

    kept = assembled.sections[0]
    assert kept.truncated is True
    assert kept.tokens <= 500
    assert assembled.notices == ()


def test_a_section_below_its_floor_is_dropped_whole_rather_than_shortened() -> None:
    budget = one_band(Band.tools, 500)

    assembled = allocate([section("log", filler(1_000), floor=800)], budget, COUNTER)

    assert assembled.sections == ()
    assert assembled.by_band[Band.tools] == 0
    assert len(assembled.notices) == 1
    assert "below its floor of 800 tokens" in assembled.notices[0]


def test_a_shortened_section_confesses_the_exact_counts_where_the_model_reads_them() -> None:
    budget = one_band(Band.tools, 1_200)

    assembled = allocate([section("diff", filler(4_310))], budget, COUNTER)

    kept = assembled.sections[0]
    assert re.search(r"showing [\d,]+ of 4,310 tokens", kept.body)
    assert re.search(r"showing [\d,]+ of 4,310 tokens", kept.notice)
    assert "of 4,310 tokens" in assembled.text()


def test_a_dropped_section_leaves_a_notice_that_names_it_and_says_what_it_cost() -> None:
    budget = one_band(Band.tools, 10)

    assembled = allocate([section("the-build-log", filler(4_310), floor=4_000)], budget, COUNTER)

    assert assembled.notices == (
        "the-build-log was dropped whole from the tools band: it needs 4,310 tokens, "
        "only 1 would fit, and that is below its floor of 4,000 tokens.",
    )


def test_a_section_shortened_to_nothing_is_dropped_because_a_label_is_not_a_truth() -> None:
    budget = one_band(Band.tools, 9)

    assembled = allocate([section("log", filler(1_000))], budget, COUNTER)

    assert assembled.sections == ()
    assert "below its floor of 1 tokens" in assembled.notices[0]


# --------------------------------------------------------------------------------------
# What a trim keeps, and where it cuts.
# --------------------------------------------------------------------------------------


def test_a_long_section_keeps_both_its_head_and_its_tail() -> None:
    budget = one_band(Band.tools, 1_000)

    assembled = allocate([section("log", rows(300))], budget, COUNTER)

    body = assembled.sections[0].body
    assert "line 0000" in body
    assert "line 0299" in body
    assert "line 0150" not in body
    assert body.index("line 0000") < body.index(MARKER) < body.index("line 0299")


def test_a_section_with_room_for_only_one_fragment_keeps_its_head() -> None:
    budget = one_band(Band.tools, 20)
    assert 20 * CHARS_PER_TOKEN < HEAD_TAIL_MINIMUM_CHARS

    assembled = allocate([section("log", rows(300))], budget, COUNTER)

    body = assembled.sections[0].body
    assert body.startswith("line 0000")
    assert "line 0299" not in body
    assert body.endswith("tokens ...]")
    assert assembled.by_band[Band.tools] <= 20


def test_a_cut_lands_on_a_line_boundary_when_the_fragment_has_one() -> None:
    budget = one_band(Band.tools, 600)
    body = rows(40, width=200)

    assembled = allocate([section("log", body)], budget, COUNTER)

    shown = [line for line in assembled.sections[0].body.splitlines() if MARKER not in line]
    assert shown
    assert all(line in body.splitlines() for line in shown)


def test_a_cut_falls_back_to_characters_when_there_is_no_line_boundary() -> None:
    budget = one_band(Band.tools, 500)

    assembled = allocate([section("json", filler(2_000))], budget, COUNTER)

    head, marker, tail = assembled.sections[0].body.split("\n")
    assert set(head) == {"x"}
    assert set(tail) == {"x"}
    assert marker.startswith(MARKER)


def test_one_enormous_line_is_cut_where_the_budget_ran_out_not_at_its_only_boundary() -> None:
    """The shape of a real tool result: a short header, then minified JSON on one line.

    Snapping each cut to the only boundary its fragment contains would keep the four-byte
    header and the two-byte trailer -- three tokens out of an allowance of four thousand --
    and then, with any floor at all, drop the section for sitting under it.
    """
    budget = one_band(Band.tools, 4_000)
    body = "hdr\n" + "z" * 40_000 + "\ntl"
    assert COUNTER.count(body) > 4_000

    assembled = allocate([section("result", body, floor=50)], budget, COUNTER)

    kept = assembled.sections[0]
    assert assembled.notices == ()
    assert kept.truncated is True
    assert 3_900 < kept.tokens <= 4_000
    assert kept.body.count("z") > 15_000


def test_a_line_boundary_is_still_preferred_while_it_leaves_most_of_the_fragment() -> None:
    """The concession above is a floor on what a boundary may cost, not its repeal."""
    budget = one_band(Band.tools, 600)
    body = rows(40, width=200)

    assembled = allocate([section("log", body)], budget, COUNTER)

    shown = [line for line in assembled.sections[0].body.splitlines() if MARKER not in line]
    assert len(shown) > 5
    assert all(line in body.splitlines() for line in shown)


# --------------------------------------------------------------------------------------
# The accounting, and the edges.
# --------------------------------------------------------------------------------------


def test_no_band_total_ever_exceeds_its_allocation() -> None:
    budget = Budget(window=1_000)
    sections = [
        section(f"s{index}", filler(900), band=band) for index, band in enumerate(WRITABLE_BANDS)
    ]

    assembled = allocate(sections, budget, COUNTER)

    for band in WRITABLE_BANDS:
        assert assembled.by_band[band] <= budget.allocation(band)


def test_the_total_never_exceeds_the_usable_window() -> None:
    budget = Budget(window=1_000)
    sections = [
        section(f"s{index}", filler(900), band=band) for index, band in enumerate(WRITABLE_BANDS)
    ]

    assembled = allocate(sections, budget, COUNTER)

    assert budget.usable == 870
    assert assembled.total <= budget.usable
    assert assembled.total == sum(assembled.by_band.values())


def test_by_band_reports_the_real_post_trim_totals() -> None:
    budget = Budget(window=1_000)
    sections = [
        section("tool", filler(4_000), band=Band.tools),
        section("turns", filler(4_000), band=Band.history),
        section("small", filler(2), band=Band.system),
    ]

    assembled = allocate(sections, budget, COUNTER)

    for band in WRITABLE_BANDS:
        kept = assembled.band(band)
        assert assembled.by_band[band] == sum(part.tokens for part in kept)
        assert assembled.by_band[band] == sum(COUNTER.count(part.body) for part in kept)


def test_the_allocator_recounts_what_a_producer_claimed_about_a_section() -> None:
    budget = one_band(Band.tools, 5_000)
    lying = Section(id="lying", band=Band.tools, body=filler(1_000), tokens=7)

    assembled = allocate([lying], budget, COUNTER)

    assert assembled.sections[0].tokens == 1_000
    assert assembled.by_band[Band.tools] == 1_000


def test_shares_that_promise_more_than_the_window_are_scaled_down_together() -> None:
    budget = Budget(window=1_000, shares={Band.system: 1.0, Band.tools: 1.0})
    sections = [
        section("prompt", filler(1_000), band=Band.system),
        section("result", filler(1_000), band=Band.tools),
    ]

    assembled = allocate(sections, budget, COUNTER)

    assert assembled.by_band[Band.system] <= 500
    assert assembled.by_band[Band.tools] <= 500
    assert assembled.total <= budget.usable


def test_a_section_in_the_reserve_band_is_a_programming_error() -> None:
    budget = Budget(window=1_000)
    stray = section("this-turns-reply", filler(10), band=Band.reserve)

    with pytest.raises(ValueError, match="reserve is kept empty on purpose") as raised:
        allocate([stray], budget, COUNTER)

    assert "this-turns-reply" in str(raised.value)


def test_a_section_whose_band_is_a_bare_string_lands_in_that_band_and_not_in_none() -> None:
    """`Band` is a `StrEnum`, so a section rebuilt from stored JSON carries "tools".

    Every bucket in the allocator is an identity test. An unresolved band is therefore not a
    section in the wrong band; it is a section in no band at all, which leaves no section
    and no notice -- the one outcome the accounting in this module exists to rule out.
    """
    budget = one_band(Band.tools, 1_000)
    from_json = Section(id="ghost", band="tools", body=filler(4_000), tokens=0)  # type: ignore[arg-type]

    assembled = allocate([from_json], budget, COUNTER)

    assert len(assembled.sections) + len(assembled.notices) == 1
    assert assembled.by_band[Band.tools] > 0
    assert assembled.band(Band.tools) == assembled.sections


def test_the_reserve_is_refused_however_the_caller_spelled_the_band() -> None:
    stray = Section(id="this-turns-reply", band="reserve", body=filler(10), tokens=0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="reserve is kept empty on purpose"):
        allocate([stray], Budget(window=1_000), COUNTER)


def test_a_band_that_names_nothing_is_refused_and_the_message_lists_the_ones_that_exist() -> None:
    stray = Section(id="mystery", band="toolz", body=filler(10), tokens=0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="which is not a band") as raised:
        allocate([stray], Budget(window=1_000), COUNTER)

    message = str(raised.value)
    assert "mystery" in message
    assert "toolz" in message
    assert all(band.value in message for band in Band)


def test_no_sections_at_all_produce_an_empty_assembly_rather_than_an_error() -> None:
    assembled = allocate([], Budget(window=1_000), COUNTER)

    assert assembled.sections == ()
    assert assembled.notices == ()
    assert assembled.total == 0
    assert assembled.by_band == dict.fromkeys(WRITABLE_BANDS, 0)
    assert Band.reserve not in assembled.by_band


def test_a_zero_window_drops_everything_and_says_so_once_per_section() -> None:
    assembled = allocate(
        [section("a", filler(10)), section("b", filler(10), band=Band.pinned)],
        Budget(window=0),
        COUNTER,
    )

    assert assembled.sections == ()
    assert assembled.total == 0
    assert len(assembled.notices) == 2


def test_a_section_larger_than_its_whole_band_is_shortened_rather_than_refused() -> None:
    budget = one_band(Band.tools, 100)

    assembled = allocate([section("enormous", filler(5_000))], budget, COUNTER)

    assert assembled.sections[0].truncated is True
    assert assembled.by_band[Band.tools] <= 100


def test_a_section_that_costs_nothing_is_never_given_up() -> None:
    budget = one_band(Band.tools, 500)
    sections = [
        section("empty", "", priority=99),
        section("large", filler(1_000), priority=0),
    ]

    assembled = allocate(sections, budget, COUNTER)

    assert [kept.id for kept in assembled.sections] == ["empty", "large"]
    assert assembled.sections[0].tokens == 0
    assert assembled.sections[1].truncated is True


def test_sections_come_back_in_the_order_they_arrived_in() -> None:
    budget = Budget(window=4_000)
    sections = [
        section("first", filler(10), band=Band.tools),
        section("second", filler(10), band=Band.pinned),
        section("third", filler(10), band=Band.tools),
        section("fourth", filler(10), band=Band.history),
    ]

    assembled = allocate(sections, budget, COUNTER)

    assert [kept.id for kept in assembled.sections] == ["first", "second", "third", "fourth"]


def test_a_counter_that_disagrees_with_the_estimate_still_never_overspends() -> None:
    counter = Words()
    budget = one_band(Band.tools, 40)
    body = "\n".join(f"word{index} and some more words here" for index in range(200))

    assembled = allocate([Section(id="log", band=Band.tools, body=body, tokens=0)], budget, counter)

    assert assembled.by_band[Band.tools] <= 40
    assert counter.count(assembled.sections[0].body) <= 40


def test_a_section_is_dropped_when_not_even_its_confession_would_fit() -> None:
    budget = one_band(Band.tools, 500)

    assembled = allocate(
        [Section(id="log", band=Band.tools, body=rows(50), tokens=0)],
        budget,
        Expensive(),
    )

    assert assembled.sections == ()
    assert assembled.notices[0].startswith("log was dropped whole")


# --------------------------------------------------------------------------------------
# Properties, over generated input.
# --------------------------------------------------------------------------------------


def generated(seed: int) -> tuple[list[Section], Budget]:
    """A deterministic pile of sections and a budget, from one seed.

    `tokens` is deliberately a random lie: the allocator recounts, and a property that only
    held for honest producers would not be worth much.
    """
    rng = random.Random(seed)  # noqa: S311 - generating test input, not a secret
    bands = list(WRITABLE_BANDS)
    sections = []
    for index in range(rng.randrange(0, 12)):
        body = "\n".join(
            f"row {row} of section {index} " + "y" * rng.randrange(0, 50)
            for row in range(rng.randrange(0, 25))
        )
        sections.append(
            Section(
                id=f"s{index:02d}",
                band=rng.choice(bands),
                body=body,
                tokens=rng.randrange(0, 5_000),
                priority=rng.randrange(0, 100),
                floor_tokens=rng.choice([0, 0, 0, 5, 40, 400]),
            )
        )
    window = rng.choice([0, 1, 12, 120, 1_200, 12_000])
    over_promised = dict.fromkeys(bands, 0.5)
    budget = rng.choice([Budget(window=window), Budget(window=window, shares=over_promised)])
    return sections, budget


@pytest.mark.parametrize("seed", range(60))
def test_no_band_total_ever_exceeds_its_allocation_for_any_generated_input(seed) -> None:
    sections, budget = generated(seed)

    assembled = allocate(sections, budget, COUNTER)

    for band, total in assembled.by_band.items():
        assert total <= budget.allocation(band)
        assert total == sum(kept.tokens for kept in assembled.band(band))
    assert assembled.total <= budget.usable


@pytest.mark.parametrize("seed", range(60))
def test_nothing_vanishes_unaccounted_for_from_any_generated_input(seed) -> None:
    sections, budget = generated(seed)

    assembled = allocate(sections, budget, COUNTER)

    assert len(assembled.sections) + len(assembled.notices) == len(sections)
    survived = {kept.id for kept in assembled.sections}
    named = {
        given.id
        for given in sections
        if any(notice.startswith(f"{given.id} ") for notice in assembled.notices)
    }
    assert survived | named == {given.id for given in sections}
