"""A model going in circles is told so, and work that only looks like a circle is left alone.

The module that notices a repeated call was written with the loop and never connected to it.
The loop recorded every call and said nothing: `notice_for` had no caller, and because a step's
result carries no arguments, every call to one operation was recorded as the same call. Seen on
the weakest model, which read the same file four times in one turn and was never told.
"""

from __future__ import annotations

from typing import Any

from test_turn_loop import Prompts, Transcript, executor, ok_result

from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.repetition import NOTICE_AT, Repetition, fingerprint
from lucy_api.turn.window import inputs_of

READ = {"steps": [{"id": "hits", "op": "research.search", "input": {"query": "tour dates"}}]}
OTHER = {"steps": [{"id": "hits", "op": "research.search", "input": {"query": "venues"}}]}


def test_the_same_call_written_two_different_ways_is_the_same_call() -> None:
    first = fingerprint("notes.search", {"q": "tea", "limit": 5})
    second = fingerprint("notes.search", {"limit": 5, "q": "tea"})
    assert first == second


def test_different_arguments_or_a_different_operation_are_a_different_call() -> None:
    assert fingerprint("notes.search", {"q": "tea"}) != fingerprint("notes.search", {"q": "milk"})
    assert fingerprint("notes.search", {"q": "tea"}) != fingerprint("research.search", {"q": "tea"})


def test_a_value_that_is_not_json_still_fingerprints_rather_than_raising() -> None:
    assert fingerprint("workspace.write", {"data": object()})


def test_nothing_is_said_the_first_time() -> None:
    assert Repetition().record("notes.search", {"q": "tea"}, "three notes") == ""


def test_the_same_answer_again_gets_a_sentence_that_leaves_the_answer_out() -> None:
    repetition = Repetition()
    for _ in range(NOTICE_AT - 1):
        repetition.record("notes.search", {"q": "tea"}, "three notes")
    said = repetition.record("notes.search", {"q": "tea"}, "three notes")

    assert said.startswith(f"You have called notes.search with the same arguments {NOTICE_AT}")
    assert "three notes" not in said


def test_a_call_whose_answer_keeps_changing_is_work_not_a_loop() -> None:
    """Polling a job makes the same call every time, and is told apart by its answer."""
    repetition = Repetition()
    said = [repetition.record("work.check", {}, f"{done} of 5 done") for done in range(1, 5)]
    assert said == ["", "", "", ""]


def test_an_answer_that_changed_starts_the_count_again() -> None:
    repetition = Repetition()
    repetition.record("workspace.read", {"path": "a.txt"}, "one")
    repetition.record("workspace.read", {"path": "a.txt"}, "two")
    assert repetition.record("workspace.read", {"path": "a.txt"}, "two").startswith("You have")
    assert "3 times" in repetition.record("workspace.read", {"path": "a.txt"}, "two")


def test_a_call_s_arguments_are_read_from_the_plan_the_model_wrote() -> None:
    assert inputs_of(READ) == {"hits": {"query": "tour dates"}}
    assert inputs_of({"steps": ["not a step", {"id": "bare"}]}) == {"bare": None}
    assert inputs_of("not a plan") == {}


# --- in the loop -------------------------------------------------------------------------------


async def _notices(*plans_made: dict[str, Any]) -> list[list[str]]:
    """Each executed step's notices, in the order the steps ran."""
    transcript = Transcript()
    provider = ScriptedProvider([*(plans(plan) for plan in plans_made), speaks("Done.")])
    await run_turn(
        Turn(
            provider=provider,
            assemble=Prompts().assemble,
            append=transcript.append,
            execute=executor(ok_result()),
        )
    )
    return [
        content["notices"] for kind, _role, content in transcript.items if kind == "tool_result"
    ]


async def test_the_model_is_told_when_it_repeats_a_call_and_gets_the_same_answer() -> None:
    """The bug, named: three identical searches, and nothing was ever said about it."""
    first, second, third = await _notices(READ, READ, READ)

    assert first == []
    assert second[0].startswith("You have called research.search with the same arguments 2")
    assert "3 times" in third[0]


async def test_the_same_operation_with_other_arguments_is_another_call() -> None:
    """The bug, named: a step's result carries no arguments, so these were one call."""
    assert await _notices(READ, OTHER) == [[], []]
