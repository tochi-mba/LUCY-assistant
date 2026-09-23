"""The loop behind a watch: look, and if it is not there yet, look again in a while.

What is pinned is what a watch must never do: spin, give up on the first bad check, keep
going on a probe that is plainly broken, or hand the whole file back as the evidence.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from lucy_api.work.watch import (
    DEFAULT_EVERY_SECONDS,
    DEFAULT_FOR_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
    MAX_EVERY_SECONDS,
    MAX_EXCERPT,
    MAX_FOR_SECONDS,
    MAX_PATTERN,
    MIN_EVERY_SECONDS,
    Check,
    ProbeBrokenError,
    clamp_every,
    clamp_for,
    clip,
    compile_pattern,
    excerpt_around,
    watch,
)


class Scripted:
    """A probe that answers from a script, and a sleep that only takes notes."""

    def __init__(self, *answers: Check | Exception) -> None:
        self.answers = list(answers)
        self.slept: list[float] = []
        self.progress: list[str] = []

    async def probe(self) -> Check:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def note(self, line: str) -> None:
        self.progress.append(line)


async def run(scripted: Scripted, *, every: float = 15.0) -> dict[str, Any]:
    return await watch(
        scripted.probe, every_seconds=every, progress=scripted.note, sleep=scripted.sleep
    )


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


async def test_it_fires_on_the_first_check_that_says_yes_and_carries_the_facts() -> None:
    scripted = Scripted(
        Check(fired=False, detail="exit 1"),
        Check(fired=True, detail="exit 0", excerpt="all green", facts={"exit_code": 0}),
    )

    result = await run(scripted)

    assert result == {"fired": True, "checks": 2, "excerpt": "all green", "exit_code": 0}
    assert scripted.slept == [15.0], "it slept once, between the two checks, and not after"


async def test_the_live_block_learns_what_the_last_check_said_and_when_the_next_one_is() -> None:
    scripted = Scripted(
        Check(fired=False, detail="answered 404"),
        Check(fired=False),
        Check(fired=True),
    )

    await run(scripted, every=30.0)

    assert scripted.progress == [
        "checked 1x, answered 404; next in 30s",
        "checked 2x, not yet; next in 30s",
    ]


async def test_one_failed_check_is_a_note_not_an_ending() -> None:
    """The workspace blipped. The watch carries on and says so, in one line."""
    scripted = Scripted(ConnectionError("gone"), Check(fired=True))

    result = await run(scripted)

    assert result["checks"] == 2
    assert scripted.progress == ["checked 1x, check failed (ConnectionError); next in 15s"]


async def test_five_failures_in_a_row_is_a_broken_probe_and_the_sentence_names_the_error() -> None:
    scripted = Scripted(*[ValueError("bad") for _ in range(MAX_CONSECUTIVE_FAILURES)])

    with pytest.raises(ProbeBrokenError) as raised:
        await run(scripted)

    assert str(raised.value) == f"{MAX_CONSECUTIVE_FAILURES} checks in a row failed (ValueError)"
    assert len(scripted.slept) == MAX_CONSECUTIVE_FAILURES - 1


async def test_a_good_check_between_failures_resets_the_count() -> None:
    """Four failures, one honest 'not yet', four more failures: still watching."""
    scripted = Scripted(
        *[OSError("x") for _ in range(MAX_CONSECUTIVE_FAILURES - 1)],
        Check(fired=False, detail="not there"),
        *[OSError("x") for _ in range(MAX_CONSECUTIVE_FAILURES - 1)],
        Check(fired=True),
    )

    result = await run(scripted)

    assert result["checks"] == 2 * MAX_CONSECUTIVE_FAILURES


async def test_the_excerpt_is_bounded_even_when_the_probe_was_generous() -> None:
    scripted = Scripted(Check(fired=True, excerpt="y" * (MAX_EXCERPT * 3)))

    result = await run(scripted)

    assert len(result["excerpt"]) == MAX_EXCERPT
    assert result["excerpt"].endswith(" […]")


# --------------------------------------------------------------------------------------
# The helpers the pack leans on
# --------------------------------------------------------------------------------------


def test_no_pattern_is_none_and_a_pattern_is_multiline() -> None:
    assert compile_pattern("") is None
    compiled = compile_pattern("^done$")
    assert compiled is not None
    assert compiled.search("start\ndone\n") is not None


def test_a_pattern_that_is_a_program_or_not_a_pattern_is_refused_in_words() -> None:
    with pytest.raises(ValueError, match=f"keep it under {MAX_PATTERN}"):
        compile_pattern("a" * (MAX_PATTERN + 1))
    with pytest.raises(ValueError, match="not a valid regular expression"):
        compile_pattern("(unclosed")


def test_the_excerpt_is_the_tail_without_a_match_and_the_surroundings_with_one() -> None:
    long = "line\n" * 1_000 + "VERDICT: pass\n" + "trailer\n" * 400
    assert excerpt_around(long, None) == long[-MAX_EXCERPT:]

    found = re.search("VERDICT: pass", long)
    around = excerpt_around(long, found)
    assert "VERDICT: pass" in around
    assert len(around) <= MAX_EXCERPT + len("VERDICT: pass")

    short = "just this"
    assert excerpt_around(short, re.search("this", short)) == short


def test_clip_leaves_short_text_alone_and_marks_a_cut() -> None:
    assert clip("fine") == "fine"
    cut = clip("z" * (MAX_EXCERPT + 1))
    assert len(cut) == MAX_EXCERPT
    assert cut.endswith(" […]")


def test_the_interval_and_the_lifetime_are_clamped_and_never_trust_a_bool() -> None:
    assert clamp_every(None) == DEFAULT_EVERY_SECONDS
    assert clamp_every(True) == DEFAULT_EVERY_SECONDS
    assert clamp_every("soon") == DEFAULT_EVERY_SECONDS
    assert clamp_every(0) == MIN_EVERY_SECONDS
    assert clamp_every(10_000) == MAX_EVERY_SECONDS
    assert clamp_every(42) == 42.0

    assert clamp_for(None) == DEFAULT_FOR_SECONDS
    assert clamp_for(-5) == MIN_EVERY_SECONDS
    assert clamp_for(10**9) == MAX_FOR_SECONDS
    assert clamp_for(90.5) == 90.5
