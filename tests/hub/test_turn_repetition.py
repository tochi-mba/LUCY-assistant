"""Noticing a model going in circles, and saying something useful about it.

The behaviour worth defending is the restraint: a notice before a block. Polling a job,
re-reading a file another step just wrote, and retrying something genuinely transient all
look identical to a loop from outside, and the only thing that tells them apart is whether
the answer is changing.
"""

from __future__ import annotations

from lucy_api.turn.repetition import DROP_AT, NOTICE_AT, Repetition, fingerprint


def test_the_same_call_written_two_different_ways_is_the_same_call() -> None:
    """A model that reorders its own JSON keys between attempts is still repeating itself."""
    first = fingerprint("research.search", {"query": "tea", "limit": 5})
    second = fingerprint("research.search", {"limit": 5, "query": "tea"})
    assert first == second


def test_different_arguments_are_a_different_call() -> None:
    assert fingerprint("research.search", {"query": "tea"}) != fingerprint(
        "research.search", {"query": "coffee"}
    )


def test_the_same_arguments_to_a_different_operation_are_a_different_call() -> None:
    assert fingerprint("research.search", {"q": 1}) != fingerprint("notes.search", {"q": 1})


def test_a_value_that_is_not_json_still_fingerprints_rather_than_raising() -> None:
    assert fingerprint("workspace.write", {"when": object()})


def test_nothing_is_said_the_first_time() -> None:
    """Calling something once is not a pattern, and a notice on the first call is noise."""
    seen = Repetition()
    seen.record("research.search", {"q": "tea"}, "three results")

    assert seen.notice_for("research.search", {"q": "tea"}) == ""


def test_the_second_identical_call_gets_a_sentence_naming_what_came_back() -> None:
    """ "You already did this" without "and it said X" leaves the model no wiser than before."""
    seen = Repetition()
    for _ in range(NOTICE_AT):
        seen.record("research.search", {"q": "tea"}, "no results for tea")

    notice = seen.notice_for("research.search", {"q": "tea"})

    assert "already called research.search" in notice
    assert "no results for tea" in notice
    assert "change the arguments" in notice, "it says what to do instead"


def test_a_notice_comes_well_before_a_withdrawal() -> None:
    """The notice should have room to work; dropping a tool the model needs is worse."""
    assert NOTICE_AT < DROP_AT

    seen = Repetition()
    for _ in range(NOTICE_AT):
        seen.record("research.search", {"q": "tea"}, "nothing")

    assert seen.notice_for("research.search", {"q": "tea"}) != ""
    assert seen.should_drop("research.search", {"q": "tea"}) is False


def test_an_operation_that_ignores_the_notice_is_withdrawn_for_the_turn() -> None:
    seen = Repetition()
    for _ in range(DROP_AT):
        seen.record("research.search", {"q": "tea"}, "nothing")

    assert seen.should_drop("research.search", {"q": "tea"}) is True

    seen.drop("research.search")
    assert seen.is_dropped("research.search") is True
    assert seen.is_dropped("notes.search") is False


def test_what_was_withdrawn_is_reported_rather_than_silently_missing() -> None:
    seen = Repetition()
    assert seen.summary() == ""

    seen.drop("research.search")
    seen.drop("music.play")
    summary = seen.summary()

    assert "music.play, research.search" in summary, "named, and in a stable order"
    assert "repeated identical calls" in summary


def test_how_many_times_something_was_tried_is_answerable() -> None:
    seen = Repetition()
    seen.record("notes.search", {"q": "tea"}, "one")
    seen.record("notes.search", {"q": "tea"}, "one")

    assert seen.seen("notes.search", {"q": "tea"}) == 2
    assert seen.seen("notes.search", {"q": "coffee"}) == 0


def test_the_latest_outcome_is_the_one_reported() -> None:
    """A call whose answer changed is not a loop, and the notice must not claim it is."""
    seen = Repetition()
    seen.record("media.status", {"job": "1"}, "queued")
    seen.record("media.status", {"job": "1"}, "downloading")

    assert "downloading" in seen.notice_for("media.status", {"job": "1"})
    assert "queued" not in seen.notice_for("media.status", {"job": "1"})
