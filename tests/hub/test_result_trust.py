"""A step's result is framed by where it came from, not all of it as an attack surface.

Read in the requests the hub actually sent: a fact the person had just told Lucy came back from
`notes.setFact` framed as "An UNTRUSTED result ... This came from somewhere an attacker can
write" -- the warning meant for web pages, on the person's own words, every time.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_turn_loop import Prompts, Transcript, executor, ok_result

from lucy_api.context.framing import OWN_OPERATIONS, UNTRUSTED_RESULT_CLOSING, result_trust
from lucy_api.context.types import Trust
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.turn.loop import Turn, run_turn

FACT = {"id": "mem_1", "title": "Weekly review", "body": "Friday afternoons", "trust": "stated"}


def test_the_person_s_own_note_is_not_framed_as_an_attack() -> None:
    """The bug, named."""
    assert result_trust("notes.setFact", FACT) is Trust.stated


@pytest.mark.parametrize(
    ("data", "trust"),
    [
        ({"facts": [FACT, {**FACT, "trust": "inferred"}], "blocks": []}, Trust.inferred),
        ({"facts": [{**FACT, "trust": "observed"}]}, Trust.observed),
        ([FACT, {**FACT, "trust": "untrusted"}], Trust.untrusted),
        ({"facts": [{**FACT, "trust": "made-up"}]}, Trust.untrusted),
        ({"kinds": {"fact": "A durable claim."}}, Trust.observed),
    ],
)
def test_a_notes_result_is_as_trusted_as_its_least_trusted_memory(data: Any, trust: Trust) -> None:
    assert result_trust("notes.aboutMe", data) is trust


@pytest.mark.parametrize("operation", sorted(OWN_OPERATIONS))
def test_lucy_s_own_machinery_is_observed(operation: str) -> None:
    assert result_trust(operation, {"anything": "at all"}) is Trust.observed


@pytest.mark.parametrize(
    "operation",
    ["research.open", "workspace.read", "workspace.run", "work.result", "watch.command"],
)
def test_anything_an_outsider_can_write_is_still_untrusted(operation: str) -> None:
    """A page, a file, a command's output and a helper's report are downstream of what they read."""
    assert result_trust(operation, {"trust": "stated"}) is Trust.untrusted


async def test_the_model_reads_the_person_s_note_without_the_attack_warning() -> None:
    transcript, prompts = Transcript(), Prompts()
    saved = ok_result(id="keep", operation="notes.setFact", data=FACT)
    await run_turn(
        Turn(
            provider=ScriptedProvider(
                [
                    plans({"steps": [{"id": "keep", "op": "notes.setFact", "input": {}}]}),
                    speaks("Kept."),
                ]
            ),
            assemble=prompts.assemble,
            append=transcript.append,
            execute=executor(saved),
        )
    )
    [result] = [content for kind, _role, content in transcript.items if kind == "tool_result"]
    assert "A result recorded as stated" in result["summary"]
    assert UNTRUSTED_RESULT_CLOSING not in result["summary"]
