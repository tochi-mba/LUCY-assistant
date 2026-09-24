"""Every step can say what it is for, and that sentence reaches the person.

Lucy's prompt has always asked for "one sentence, in plain words, saying what that call is for"
on every step, written "for the person who will be asked to approve it", and the permission gate
has always put a step's `note` on the approval card. But the plan schema offered no such field,
with `additionalProperties: false`, and weftai's validator refuses one -- so a model that did as
the prompt said wrote a plan that could not run, and every approval card fell back to a generic
sentence about the permission.
"""

from __future__ import annotations

import asyncio
from typing import Any

from test_turn_loop import Prompts, Transcript, executor, ok_result

from lucy_api.clients.testing import FakeHttp
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.help import HelpPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope
from lucy_api.turn.loop import Turn, run_turn
from lucy_api.turn.window import NOTE, executable, notes_of

WHY = "Read how helpers work before starting one."
READ = {"steps": [{"id": "docs", "op": "help.docs", "input": {"topic": "agents"}, NOTE: WHY}]}


class Memory:
    """Enough of the memory service for notes to be ready. A parked write never reaches it."""

    async def blocks(self, *, profile: str = "") -> tuple[()]:
        del profile
        return ()


def _hub(mode: str = "auto") -> tuple[Capabilities, Any]:
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test", client=Memory())))
    context = capabilities.context_for(
        SessionScope(account_id="acct", profile="personal", session_id="ses", permission_mode=mode),
        http=FakeHttp(),
    )
    return capabilities, context


def test_a_step_that_says_what_it_is_for_runs() -> None:
    """The bug, named: this plan was refused before it ran."""

    async def run() -> dict[str, Any]:
        capabilities, context = _hub()
        await capabilities.probe(context)
        return await capabilities.execute(READ, context)

    result = asyncio.run(run())
    assert not result.get("issues")
    assert result["steps"][0]["status"] == "ok"


def test_every_operation_offers_the_field() -> None:
    async def schema() -> dict[str, Any]:
        capabilities, context = _hub()
        return capabilities.plan_schema(await capabilities.probe(context), "ses", context)

    variants = asyncio.run(schema())["properties"]["steps"]["items"]["anyOf"]
    assert all(variant["properties"][NOTE]["type"] == "string" for variant in variants)


def test_the_note_is_the_approval_card_s_sentence() -> None:
    write = {
        "steps": [
            {
                "id": "tea",
                "op": "notes.remember",
                "input": {"title": "tea", "body": "green"},
                NOTE: "Keep that you prefer tea.",
            }
        ]
    }

    async def run() -> dict[str, Any]:
        capabilities, context = _hub(mode="ask")
        await capabilities.probe(context)
        return await capabilities.execute(write, context)

    [ask] = asyncio.run(run())["issues"]
    assert ask["description"] == "Keep that you prefer tea."


async def test_the_note_reaches_the_result_the_model_and_the_log_read() -> None:
    transcript = Transcript()
    await run_turn(
        Turn(
            provider=ScriptedProvider([plans(READ), speaks("Read it.")]),
            assemble=Prompts().assemble,
            append=transcript.append,
            execute=executor(ok_result(id="docs", operation="help.docs", note="")),
        )
    )
    [result] = [content for kind, _role, content in transcript.items if kind == "tool_result"]
    assert result["note"] == WHY


def test_the_executor_never_sees_it_and_the_plan_keeps_it() -> None:
    stripped = executable(READ)
    assert NOTE not in stripped["steps"][0]
    assert READ["steps"][0][NOTE] == WHY
    assert notes_of(READ) == {"docs": WHY}


def test_notes_of_anything_but_a_plan_with_notes_is_empty() -> None:
    assert notes_of("not a plan") == {}
    assert notes_of({"steps": [{"id": "a", NOTE: "  "}, "not a step", {"id": "b"}]}) == {}
