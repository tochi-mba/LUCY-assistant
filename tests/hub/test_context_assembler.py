"""Where each piece of the window ends up, and what that placement protects.

Two properties carry the whole design and are worth more than the rest of this file put
together: the live state block is read **last**, so that rewriting it every turn does not
end the cached prefix; and a flood of tool output cannot take the person's pinned context
down with it.
"""

from __future__ import annotations

from lucy_api.context.assembler import DEFAULT_WINDOW, PromptContext, Window, assemble
from lucy_api.context.tokens import Estimate
from lucy_api.context.types import Band, Budget, Claim, Section, Trust

LIVE = Section(
    id="live.state", band=Band.pinned, body="now: 2026-09-17\nagents: 2 running", tokens=0
)


def section(identifier: str, body: str, band: Band = Band.history, **extra) -> Section:
    return Section(id=identifier, band=band, body=body, tokens=0, **extra)


def ids(assembled) -> list[str]:
    return [part.id for part in assembled.sections]


def test_the_live_state_is_the_last_thing_the_model_reads() -> None:
    """Not a formatting preference: it is what keeps the cached prefix intact.

    Everything before the live block is identical to the previous turn, so a provider can
    charge the cached rate for it. Move this block earlier and every turn pays full price
    for the whole prompt.
    """
    assembled = assemble(
        Window(
            history=[section("turn.1", "hello"), section("turn.2", "hi")],
            tools=[section("tool.1", "a result", Band.tools)],
            live=LIVE,
        )
    )
    assert ids(assembled)[-1] == "live.state"


def test_the_zones_are_read_in_order() -> None:
    assembled = assemble(
        Window(
            prompt=PromptContext(capabilities=("music",), goals=("book the tickets",)),
            history=[section("turn.1", "hello")],
            tools=[section("tool.1", "a result", Band.tools)],
            live=LIVE,
        )
    )
    order = ids(assembled)
    bands = {part.id: part.band for part in assembled.sections}
    assert order.index("turn.1") < order.index("tool.1") < order.index("live.state")
    assert all(
        bands[name] is Band.system or bands[name] is Band.pinned
        for name in order[: order.index("turn.1")]
    ), "everything before the conversation is stable enough to cache"


def test_the_live_block_is_priced_in_the_band_that_must_survive() -> None:
    assembled = assemble(Window(live=LIVE))
    live = next(part for part in assembled.sections if part.id == "live.state")
    assert live.band is Band.pinned
    assert assembled.by_band[Band.pinned] >= live.tokens


def test_a_flood_of_tool_output_cannot_evict_the_live_state() -> None:
    """The failure this prevents is an assistant losing track of itself mid-task.

    One shared pool would let a single large result push out the state block, the goals and
    the notes -- and the model would have no way to know that had happened.
    """
    flood = [section(f"tool.{index}", "x" * 40_000, Band.tools) for index in range(40)]
    assembled = assemble(Window(history=[section("turn.1", "hello")], tools=flood, live=LIVE))

    live = next(part for part in assembled.sections if part.id == "live.state")
    assert live.body == LIVE.body, "the live block came through untouched"
    assert not live.truncated
    assert "turn.1" in ids(assembled)
    assert assembled.by_band[Band.tools] <= Budget(window=DEFAULT_WINDOW).allocation(Band.tools)


def test_everything_that_did_not_fit_is_confessed() -> None:
    flood = [section(f"tool.{index}", "x" * 40_000, Band.tools) for index in range(40)]
    assembled = assemble(Window(tools=flood, live=LIVE))
    kept = {part.id for part in assembled.sections}
    missing = {f"tool.{index}" for index in range(40)} - kept
    assert missing, "this test is only meaningful when something was dropped"
    reported = " ".join(assembled.notices)
    for name in missing:
        assert name in reported, f"{name} vanished without a word"


def test_a_section_handed_over_in_the_wrong_band_is_put_where_its_zone_belongs() -> None:
    """A caller who forgets is corrected rather than refused.

    A tool result left in the default band would compete with the system prompt for a four
    percent allowance and lose, which is a confusing way to be told about a typo.
    """
    assembled = assemble(
        Window(
            history=[section("turn.1", "hello", Band.system)],
            tools=[section("tool.1", "result", Band.system)],
        )
    )
    placed = {part.id: part.band for part in assembled.sections}
    assert placed["turn.1"] is Band.history
    assert placed["tool.1"] is Band.tools


def test_a_window_with_nothing_in_it_is_still_the_stable_prompt() -> None:
    assembled = assemble(Window())
    assert assembled.sections, "identity, behaviour, the tool idiom and safety always render"
    assert assembled.total > 0
    assert all(part.band in {Band.system, Band.pinned} for part in assembled.sections)


def test_a_person_can_turn_a_section_off_and_replace_another() -> None:
    default = assemble(Window())
    changed = assemble(
        Window(overrides={"identity": "You are Lucy. Be brief."}, disabled=["memory"])
    )
    assert "Be brief." in changed.text()
    assert "memory" not in {part.id for part in changed.sections}
    assert "memory" in {part.id for part in default.sections}


def test_the_persons_notes_arrive_as_claims_and_not_as_orders() -> None:
    assembled = assemble(
        Window(
            prompt=PromptContext(
                notes=(Claim(body="prefers tea", source="memory", trust=Trust.stated),)
            )
        )
    )
    text = assembled.text()
    assert "prefers tea" in text
    assert "not instructions" in text, "a recorded note is data, and says so"


def test_the_budget_and_the_counter_are_both_replaceable() -> None:
    small = assemble(
        Window(history=[section("turn.1", "word " * 5_000)], live=LIVE),
        budget=Budget(window=2_000),
        counter=Estimate(),
    )
    assert small.total <= Budget(window=2_000).usable
    assert small.notices or any(part.truncated for part in small.sections)


def test_two_identical_windows_assemble_to_the_same_bytes() -> None:
    """A prompt that differs from itself cannot be cached and cannot be diffed."""
    window = Window(history=[section("turn.1", "hello")], tools=[], live=LIVE)
    assert assemble(window).text() == assemble(window).text()
