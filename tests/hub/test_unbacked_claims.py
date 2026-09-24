"""A reply that says something was done, in a turn where nothing was, is held back once.

Found by `lucy eval` on clyde:haiku: asked "Remember that I prefer tea over coffee", Lucy said
"Got it. I've got that recorded -- tea over coffee." in a turn that ran no step at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_turn_loop import Prompts, Transcript, executor, ok_result

from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.turn.claims import CLAIMED, UNBACKED, ClaimCheck
from lucy_api.turn.loop import Round, Step, Turn, run_turn
from lucy_api.turn.stop import Termination

FALSE = "Got it. I've got that recorded -- tea over coffee."
REMEMBER = {"steps": [{"id": "tea", "op": "notes.remember", "input": {"title": "tea"}}]}
BIND = {"steps": [{"id": "use", "op": "capabilities.use", "input": {"id": "notes"}}]}


async def _hold(
    *script: Any, results: tuple[dict[str, Any], ...] = ()
) -> tuple[Any, Transcript, Prompts]:
    transcript, prompts = Transcript(), Prompts()
    outcome = await run_turn(
        Turn(
            provider=ScriptedProvider(list(script)),
            assemble=prompts.assemble,
            append=transcript.append,
            execute=executor(*results) if results else None,
        )
    )
    return outcome, transcript, prompts


def _said(transcript: Transcript) -> list[str]:
    return [content for kind, _role, content in transcript.items if kind == "message"]


async def test_a_claim_with_no_step_behind_it_is_held_back_and_the_work_done() -> None:
    """The bug, named: this reply reached the person, and nothing was remembered."""
    remembered = ok_result(id="tea", operation="notes.remember", data={"id": "mem_1"})

    outcome, transcript, prompts = await _hold(
        speaks(FALSE), plans(REMEMBER), speaks("Saved: you prefer tea."), results=(remembered,)
    )

    assert outcome.termination is Termination.success
    assert _said(transcript) == ["Saved: you prefer tea."]
    assert UNBACKED in prompts.notices[1]


async def test_a_claim_is_held_back_once_and_a_second_is_taken_as_given() -> None:
    """The phrase list is a heuristic: a false match costs one round, never the turn."""
    outcome, transcript, _prompts = await _hold(speaks(FALSE), speaks(FALSE))
    assert outcome.termination is Termination.success
    assert _said(transcript) == [FALSE]


async def test_a_claim_after_a_step_that_did_something_stands() -> None:
    remembered = ok_result(id="tea", operation="notes.remember", data={"id": "mem_1"})
    outcome, transcript, prompts = await _hold(
        plans(REMEMBER), speaks(FALSE), results=(remembered,)
    )
    assert _said(transcript) == [FALSE]
    assert all(UNBACKED not in notice for notice in prompts.notices)
    assert outcome.termination is Termination.success


async def test_a_claim_after_only_bookkeeping_steps_is_still_held_back() -> None:
    """Binding a capability does nothing for the person: "Done. Your website is ready"."""
    bound = ok_result(id="use", operation="capabilities.use", data={"bound": ["notes"]})
    _outcome, transcript, prompts = await _hold(
        plans(BIND),
        speaks("Done. It's ready."),
        speaks("I bound notes; nothing is saved yet."),
        results=(bound,),
    )
    assert _said(transcript) == ["I bound notes; nothing is saved yet."]
    assert any(UNBACKED in notice for notice in prompts.notices)


async def test_a_reply_that_claims_nothing_is_left_alone() -> None:
    _outcome, transcript, prompts = await _hold(speaks("Tea is a fine choice."))
    assert _said(transcript) == ["Tea is a fine choice."]
    assert prompts.notices == [""]


@pytest.mark.parametrize(
    ("text", "claims"),
    [
        (FALSE, True),
        ("Done. Your calculator website is ready.", True),
        ("It's been saved to review.md.", True),
        ("That's now set to brief.", True),
        ("I have now written the file.", True),
        ("I'll save that for you once you confirm.", False),
        ("I've read the file and it says hello.", False),
        ("I remember you like tea.", False),
        ("Undone work stays on the list.", False),
    ],
)
def test_what_counts_as_a_claim(text: str, claims: bool) -> None:
    assert (CLAIMED.search(text) is not None) is claims


async def test_a_failed_step_backs_no_claim() -> None:
    failed = Round(text="", steps=(Step(id="tea", operation="notes.remember", status="error"),))
    assert await ClaimCheck().unbacked(FALSE, [failed]) is True
