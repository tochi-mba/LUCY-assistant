"""The live-state block's contract as an assembled prompt section."""

from test_context_state import Chars, a_crowd, a_state, body_of, render

from lucy_api.context.state import SECTION_ID, SECTION_PRIORITY, SECTION_TITLE
from lucy_api.context.types import Band


def test_the_block_is_one_pinned_section_that_is_given_up_last() -> None:
    section = render(a_state())

    assert section.id == SECTION_ID
    assert section.title == SECTION_TITLE
    assert section.band is Band.pinned
    assert section.priority == SECTION_PRIORITY == 0


def test_the_section_reports_its_own_cost_and_the_floor_below_which_it_is_not_worth_keeping() -> (
    None
):
    counter = Chars()
    section = render(a_crowd(), counter=counter)

    assert section.tokens == counter.count(section.body)
    assert section.floor_tokens == render(a_crowd(), limit=0, counter=counter).tokens


def test_the_same_state_renders_the_same_bytes_so_two_turns_can_be_diffed() -> None:
    assert body_of(a_crowd()) == body_of(a_crowd())
    assert body_of(a_crowd(), limit=260) == body_of(a_crowd(), limit=260)


def test_the_block_opens_and_closes_with_a_delimiter_so_its_edges_are_unambiguous() -> None:
    lines = body_of(a_state()).splitlines()

    assert lines[0].startswith("--- live state")
    assert lines[-1] == "--- end live state ---"
