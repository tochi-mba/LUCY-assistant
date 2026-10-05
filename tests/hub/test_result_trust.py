"""A step's result is framed by where it came from, and the pack that produced it says where.

Read in the requests the hub actually sent: a fact the person had just told Lucy came back from
`notes.setFact` framed as "An UNTRUSTED result ... This came from somewhere an attacker can
write" -- the warning meant for web pages, on the person's own words, every time.

The first fix kept a list, in the context layer, of every operation whose results were Lucy's
own. It had to be kept by hand, far from the packs that define the operations, and a new
operation was whatever the list's author remembered. Each pack now answers for its own.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_turn_loop import Prompts, Transcript, executor, ok_result

from lucy_api.clients.persona import FakePersonaClient
from lucy_api.context.framing import as_trust
from lucy_api.context.types import Trust
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.help import HelpPack
from lucy_api.packs.mcp import McpPack
from lucy_api.packs.music import MusicPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.research import ResearchPack
from lucy_api.packs.service import Capabilities
from lucy_api.packs.settings import SettingsPack
from lucy_api.packs.watch import WatchPack
from lucy_api.packs.work import WorkPack
from lucy_api.packs.workspace import WorkspacePack
from lucy_api.sessions.scope import SessionScope
from lucy_api.turn.loop import Turn, run_turn

FACT = {"id": "mem_1", "title": "Weekly review", "body": "Friday afternoons", "trust": "stated"}
NOTES = NotesPack("http://memory.test")


def test_the_person_s_own_note_is_not_framed_as_an_attack() -> None:
    """The bug, named."""
    assert NOTES.result_trust("notes.setFact", FACT) is Trust.stated


@pytest.mark.parametrize(
    ("data", "trust"),
    [
        ({"facts": [FACT, {**FACT, "trust": "inferred"}], "blocks": []}, Trust.inferred),
        ({"facts": [{**FACT, "trust": "observed"}]}, Trust.observed),
        ([FACT, {**FACT, "trust": "untrusted"}], Trust.untrusted),
        ({"facts": [{**FACT, "trust": "made-up"}]}, Trust.untrusted),
        ({"kinds": {"fact": "A durable claim."}}, Trust.observed),
        ({"lesson_id": "note_1", "lesson": "Summary first.", "revision": 1}, Trust.observed),
    ],
)
def test_a_notes_result_is_as_trusted_as_its_least_trusted_memory(data: Any, trust: Trust) -> None:
    assert NOTES.result_trust("notes.aboutMe", data) is trust


@pytest.mark.parametrize(
    ("pack", "operation"),
    [
        (HelpPack(), "capabilities.list"),
        (HelpPack(), "help.docs"),
        (SettingsPack("http://settings.test"), "settings.set"),
        (WorkPack(), "work.check"),
        (WorkPack(), "work.cancel"),
        (AgentsPack(), "agents.list"),
        (AgentsPack(), "journal.claim"),
        (WorkspacePack("http://workspace.test"), "workspace.write"),
        (WorkspacePack("http://workspace.test"), "workspace.delete"),
    ],
)
def test_lucy_s_own_machinery_is_observed(pack: Any, operation: str) -> None:
    assert pack.result_trust(operation, {"trust": "untrusted"}) is Trust.observed


@pytest.mark.parametrize(
    ("pack", "operation"),
    [
        (ResearchPack("http://search.test"), "research.open"),
        (WorkspacePack("http://workspace.test"), "workspace.read"),
        (WorkspacePack("http://workspace.test"), "workspace.list"),
        (WorkspacePack("http://workspace.test"), "workspace.run"),
        (WorkspacePack("http://workspace.test"), "workspace.script"),
        (MusicPack("http://music.test"), "music.search"),
        (WatchPack("http://workspace.test"), "watch.command"),
        (McpPack((), None), "mcp.call"),
        (WorkPack(), "work.result"),
        (AgentsPack(), "agents.read"),
    ],
)
def test_anything_an_outsider_can_write_is_untrusted_whatever_it_says(
    pack: Any, operation: str
) -> None:
    """A page, a file, a command's output and a helper's words are downstream of what they read."""
    assert pack.result_trust(operation, {"trust": "stated"}) is Trust.untrusted


@pytest.mark.parametrize(
    ("said", "trust"),
    [("stated", Trust.stated), ("made-up", Trust.untrusted), (None, Trust.untrusted)],
)
def test_a_mark_the_loop_does_not_know_is_untrusted(said: object, trust: Trust) -> None:
    assert as_trust(said) is trust


# --- the service marks each step, and the loop frames by the mark ----------------------------


async def _ran(operation: str, **inputs: str) -> dict[str, Any]:
    notes = NotesPack("http://memory.test", client=_Memory(), persona=FakePersonaClient())
    capabilities = Capabilities((HelpPack(), notes))
    context = capabilities.context_for(
        SessionScope(
            account_id="acct", profile="personal", session_id="ses", permission_mode="auto"
        )
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "s", "op": operation, "input": inputs}]}, context
    )
    step: dict[str, Any] = result["steps"][0]
    return step


async def test_each_step_is_marked_by_the_pack_that_produced_it() -> None:
    assert (await _ran("help.docs", topic="notes"))["trust"] == "observed"
    assert (await _ran("notes.learn", lesson="Summary first."))["trust"] == "observed"


async def test_a_step_that_failed_carries_no_mark() -> None:
    failed = await _ran("notes.search", query="tea")  # this memory cannot search
    assert failed["status"] == "error"
    assert "trust" not in failed


async def test_the_model_reads_the_person_s_note_without_the_attack_warning() -> None:
    transcript, prompts = Transcript(), Prompts()
    saved = ok_result(id="keep", operation="notes.setFact", data=FACT, trust="stated")
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
    assert 'trust="stated"' in result["summary"]
    assert "anyone can write" not in result["summary"]


async def test_a_result_nobody_marked_reads_as_untrusted() -> None:
    transcript, prompts = Transcript(), Prompts()
    await run_turn(
        Turn(
            provider=ScriptedProvider(
                [plans({"steps": [{"id": "hits", "op": "x.y"}]}), speaks("")]
            ),
            assemble=prompts.assemble,
            append=transcript.append,
            execute=executor(ok_result(data=FACT)),
        )
    )
    [result] = [content for kind, _role, content in transcript.items if kind == "tool_result"]
    assert 'trust="untrusted"' in result["summary"]
    assert "anyone can write" in result["summary"]


class _Memory:
    """Enough of the memory service for notes to be ready."""

    async def blocks(self, *, profile: str = "") -> tuple[()]:
        del profile
        return ()
