"""What the model wrote beside a plan is shown only once the plan has run.

Read in a sent turn, on the weakest model: "Got it. I've noted that you're a backend engineer..."
reached the person beside a write that then stopped for their approval. Nothing had been kept,
and the words would have stood had the answer been no. Asking the model not to say it, in the
schema and the prompt, did not stop it; showing only what follows a plan that ran does.
"""

from __future__ import annotations

from typing import Any

from test_turn_loop import PLAN, Transcript, executor, ok_result, turn

from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.turn.loop import Termination, run_turn

NOTED = "Got it. I've noted that."

PARKED = {
    "issues": [
        {
            "code": "permission_required",
            "message": "Remembering notes needs approval before it can run.",
            "permission": "notes.write",
            "operation": "research.search",
        }
    ],
    "steps": [],
}
DENIED = {
    "issues": [
        {
            "code": "permission_denied",
            "message": "Not allowed.",
            "permission": "notes.write",
            "operation": "research.search",
        }
    ],
    "steps": [],
}
INVALID = {"issues": [{"code": "unknown_operation", "message": "No such operation."}], "steps": []}


def _spoken(transcript: Transcript) -> list[str]:
    return [content for kind, _role, content in transcript.items if kind == "message"]


async def _after(result: dict[str, Any], *then: Any) -> tuple[Any, Transcript]:
    transcript = Transcript()
    provider = ScriptedProvider([plans(PLAN, text=NOTED), *then])
    outcome = await run_turn(turn(provider, append=transcript.append, execute=executor(result)))
    return outcome, transcript


async def test_words_beside_a_plan_that_waits_for_approval_are_not_shown() -> None:
    """The bug, named."""
    outcome, transcript = await _after(PARKED)

    assert outcome.termination is Termination.input_required
    assert NOTED not in _spoken(transcript)
    assert NOTED not in outcome.text


async def test_words_beside_a_refused_plan_are_not_shown() -> None:
    outcome, transcript = await _after(DENIED, speaks("I can't keep that."))

    assert _spoken(transcript) == ["I can't keep that."]
    assert NOTED not in outcome.text


async def test_words_beside_a_plan_that_could_not_run_are_not_shown() -> None:
    outcome, transcript = await _after(INVALID, speaks("Let me try that another way."))

    assert _spoken(transcript) == ["Let me try that another way."]
    assert NOTED not in outcome.text


async def test_words_beside_a_plan_stopped_before_it_ran_are_not_shown() -> None:
    transcript = Transcript()
    outcome = await run_turn(
        turn(
            ScriptedProvider([plans(PLAN, text=NOTED)]),
            append=transcript.append,
            execute=executor(ok_result()),
            cancelled=lambda: True,
        )
    )

    assert outcome.termination is Termination.cancelled
    assert _spoken(transcript) == []
    assert outcome.text == ""


async def test_words_beside_a_plan_that_ran_come_just_before_its_results() -> None:
    outcome, transcript = await _after(ok_result(), speaks("Three dates in March."))

    kinds = [(kind, content) for kind, _role, content in transcript.items if kind != "reasoning"]
    assert kinds[0] == ("message", NOTED)
    assert kinds[1][0] == "tool_result"
    assert outcome.text == f"{NOTED}\n\nThree dates in March."
