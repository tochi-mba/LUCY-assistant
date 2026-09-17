"""Counting is cheap, and counting the same stable prefix twice a turn is not cheap enough."""

from __future__ import annotations

import pytest

from lucy_api.context.tokens import (
    CHARS_PER_TOKEN,
    DEFAULT_CAPACITY,
    Cached,
    Estimate,
    default_counter,
    fits,
)


class Counting:
    """A counter that records every question, so a cache can be caught working."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def count(self, text: str) -> int:
        self.asked.append(text)
        return Estimate().count(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("abcd", 1),
        ("abcde", 2),
        ("x" * 4310 * CHARS_PER_TOKEN, 4310),
    ],
)
def test_the_estimate_charges_one_token_for_every_four_characters_rounding_up(text, expected):
    assert Estimate().count(text) == expected


def test_a_cache_asks_the_counter_it_wraps_once_per_distinct_text():
    inner = Counting()
    cached = Cached(inner)

    counts = [cached.count("the system prompt") for _ in range(5)]

    assert counts == [5, 5, 5, 5, 5]
    assert inner.asked == ["the system prompt"]


def test_a_cache_returns_the_same_answers_as_the_counter_it_wraps():
    cached = Cached(Estimate())

    for text in ("", "a", "a longer piece of prose that a tool might have produced"):
        assert cached.count(text) == Estimate().count(text)


def test_a_cache_remembers_a_count_of_zero_rather_than_recomputing_it():
    inner = Counting()
    cached = Cached(inner)

    assert cached.count("") == 0
    assert cached.count("") == 0
    assert inner.asked == [""]


def test_a_full_cache_evicts_the_text_it_has_gone_longest_without_using():
    inner = Counting()
    cached = Cached(inner, capacity=2)

    cached.count("first")
    cached.count("second")
    cached.count("third")

    assert cached.size == 2
    cached.count("first")
    assert inner.asked == ["first", "second", "third", "first"]


def test_using_a_remembered_text_again_saves_it_from_the_next_eviction():
    inner = Counting()
    cached = Cached(inner, capacity=2)

    cached.count("first")
    cached.count("second")
    cached.count("first")
    cached.count("third")

    cached.count("first")
    cached.count("second")

    assert inner.asked == ["first", "second", "third", "second"]


def test_a_cache_that_cannot_hold_anything_is_refused_with_a_message_saying_so():
    with pytest.raises(ValueError, match="cannot hold anything"):
        Cached(Estimate(), capacity=0)


def test_a_cache_left_at_its_default_capacity_stops_growing_there():
    """The bound is the point. An unbounded cache keyed on tool output is a slow leak."""
    cached = Cached(Estimate())

    for index in range(DEFAULT_CAPACITY * 2):
        cached.count(f"tool result {index}")

    assert cached.size == DEFAULT_CAPACITY


def test_the_default_counter_agrees_with_the_estimate_and_remembers_what_it_counted():
    counter = default_counter()

    assert counter.count("x" * 400) == Estimate().count("x" * 400)
    counter.count("x" * 400)
    counter.count("y" * 400)

    # Three questions about two distinct texts: a counter that answered each one afresh
    # would have no size to report at all.
    assert counter.size == 2


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(1, False), (2, True), (3, True)],
)
def test_fits_answers_the_question_at_the_boundary(limit, expected):
    assert fits("abcdefgh", limit, Estimate()) is expected
