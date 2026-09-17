"""What the model reads when a conversation has been compacted.

The transcript itself is never touched, so every one of these tests is really asking the
same question: given the same log and a different set of active summaries, what comes out?
"""

from __future__ import annotations

from lucy_api.context.projection import (
    NEWEST_PRIORITY,
    OLDEST_PRIORITY,
    SUMMARY_PRIORITY,
    Compaction,
    Item,
    project,
)
from lucy_api.context.tokens import Estimate
from lucy_api.context.types import Band

COUNTER = Estimate()


def conversation(turns: int = 3) -> list[Item]:
    """Two items per turn: what was asked, and what came back."""
    items: list[Item] = []
    for number in range(1, turns + 1):
        items.append(
            Item(
                id=f"i{number}a",
                seq=number * 2 - 1,
                role="user",
                body=f"question {number}",
                turn_id=f"trn_{number}",
            )
        )
        items.append(
            Item(
                id=f"i{number}b",
                seq=number * 2,
                role="assistant",
                body=f"answer {number}",
                turn_id=f"trn_{number}",
            )
        )
    return items


def bodies(projection) -> list[str]:
    return [section.body for section in projection.sections]


def test_with_no_summaries_every_entry_is_read_in_the_order_it_happened() -> None:
    projection = project(conversation(), counter=COUNTER)
    assert bodies(projection) == [
        "user: question 1",
        "assistant: answer 1",
        "user: question 2",
        "assistant: answer 2",
        "user: question 3",
        "assistant: answer 3",
    ]
    assert projection.replaced == 0
    assert projection.summaries == 0
    assert all(section.band is Band.history for section in projection.sections)


def test_the_transcript_is_read_back_in_the_order_it_was_written_however_it_arrives() -> None:
    shuffled = list(reversed(conversation(2)))
    assert bodies(project(shuffled, counter=COUNTER)) == [
        "user: question 1",
        "assistant: answer 1",
        "user: question 2",
        "assistant: answer 2",
    ]


def test_a_summary_stands_where_the_turns_it_replaced_used_to_be() -> None:
    projection = project(
        conversation(3),
        [Compaction(seq=1, summary="They introduced themselves.", covers_from=1, covers_to=4)],
        counter=COUNTER,
    )
    assert len(projection.sections) == 3
    assert "They introduced themselves." in projection.sections[0].body
    assert bodies(projection)[1:] == ["user: question 3", "assistant: answer 3"]
    assert projection.replaced == 4
    assert projection.summaries == 1


def test_a_summary_says_what_it_stands_for_so_nothing_looks_like_it_never_happened() -> None:
    projection = project(
        conversation(2),
        [Compaction(seq=1, summary="Earlier talk.", covers_from=1, covers_to=2)],
        counter=COUNTER,
    )
    body = projection.sections[0].body
    assert "summary of 2 earlier entries" in body
    assert "sequence 1 to 2" in body
    assert "transcript is unchanged" in body


def test_a_range_that_stops_mid_turn_is_widened_to_the_whole_turn() -> None:
    """Splitting a turn is how a hand-written compactor produces a prompt nobody accepts.

    Half a turn leaves the model reading an answer to a question it cannot see, and in the
    tool case it leaves a result whose call has been summarised away.
    """
    projection = project(
        conversation(3),
        [Compaction(seq=1, summary="Earlier.", covers_from=1, covers_to=3)],
        counter=COUNTER,
    )
    assert projection.replaced == 4, "the second turn was taken whole, not halved"
    assert bodies(projection)[1:] == ["user: question 3", "assistant: answer 3"]


def test_entries_belonging_to_no_turn_are_covered_exactly_as_asked() -> None:
    loose = [Item(id="x1", seq=0, role="system", body="note"), *conversation(1)]
    projection = project(
        loose, [Compaction(seq=1, summary="A note.", covers_from=0, covers_to=0)], counter=COUNTER
    )
    assert projection.replaced == 1
    assert bodies(projection)[1:] == ["user: question 1", "assistant: answer 1"]


def test_a_summary_made_redundant_by_a_later_one_is_reported_rather_than_shown_twice() -> None:
    projection = project(
        conversation(3),
        [
            Compaction(seq=1, summary="First pass.", covers_from=1, covers_to=2),
            Compaction(seq=2, summary="Second pass.", covers_from=1, covers_to=4),
        ],
        counter=COUNTER,
    )
    assert projection.summaries == 2, "both cover fresh ground, so both are kept"

    swallowed = project(
        conversation(3),
        [
            Compaction(seq=1, summary="Wide pass.", covers_from=1, covers_to=4),
            Compaction(seq=2, summary="Narrow pass.", covers_from=1, covers_to=2),
        ],
        counter=COUNTER,
    )
    assert swallowed.summaries == 1
    assert swallowed.notices == ("compaction 2 covers nothing not already summarised",)


def test_deactivating_a_summary_brings_the_turns_back_unchanged() -> None:
    """This is the whole reason compaction is a projection and not an edit."""
    compaction = Compaction(seq=1, summary="Earlier.", covers_from=1, covers_to=4)
    compacted = project(conversation(3), [compaction], counter=COUNTER)
    restored = project(
        conversation(3),
        [Compaction(seq=1, summary="Earlier.", covers_from=1, covers_to=4, active=False)],
        counter=COUNTER,
    )
    assert compacted.replaced == 4
    assert restored.replaced == 0
    assert bodies(restored) == bodies(project(conversation(3), counter=COUNTER))


def test_the_oldest_surviving_turn_is_the_first_one_given_up() -> None:
    projection = project(conversation(4), counter=COUNTER)
    priorities = [section.priority for section in projection.sections]
    assert priorities == sorted(priorities, reverse=True), "priority falls as the log advances"
    assert priorities[0] == OLDEST_PRIORITY
    assert priorities[-1] == NEWEST_PRIORITY


def test_a_summary_outranks_every_turn_because_losing_it_loses_all_of_them() -> None:
    projection = project(
        conversation(3),
        [Compaction(seq=1, summary="Earlier.", covers_from=1, covers_to=4)],
        counter=COUNTER,
    )
    summary = projection.sections[0]
    assert summary.priority == SUMMARY_PRIORITY
    assert summary.priority < min(section.priority for section in projection.sections[1:])
    assert summary.floor_tokens == summary.tokens, "a half-trimmed summary is worse than none"


def test_a_single_surviving_entry_does_not_divide_by_zero() -> None:
    projection = project([Item(id="only", seq=1, role="user", body="hello")], counter=COUNTER)
    assert len(projection.sections) == 1
    assert projection.sections[0].priority == NEWEST_PRIORITY


def test_an_empty_transcript_projects_to_nothing_rather_than_failing() -> None:
    projection = project([], counter=COUNTER)
    assert projection.sections == ()
    assert projection.replaced == 0
    assert projection.notices == ()


def test_a_summary_of_a_transcript_that_has_since_been_emptied_still_reads() -> None:
    projection = project(
        [], [Compaction(seq=1, summary="Everything.", covers_from=1, covers_to=9)], counter=COUNTER
    )
    assert projection.sections == (), "a summary covering nothing present is not shown"
    assert projection.notices == ("compaction 1 covers nothing not already summarised",)


def test_every_section_is_priced_by_the_counter_it_was_given() -> None:
    projection = project(conversation(1), counter=COUNTER)
    for section in projection.sections:
        assert section.tokens == COUNTER.count(section.body)
