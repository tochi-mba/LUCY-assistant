"""A lesson can be kept, and comes back in every later conversation.

The prompt has always said "You can write down how this person wants you to work, and read it
back in every later conversation", and nothing could. The notes schema had a `procedure` kind
that no operation wrote, and nothing loaded one back. Persona-api already kept pinned notes of
kind `lesson` and the persona feed already carried pinned notes into every turn; the hub had no
way to write one.
"""

from __future__ import annotations

from typing import Any

import pytest

from lucy_api.clients.errors import RateLimitedError
from lucy_api.clients.live_feeds import PersonaFeeds
from lucy_api.clients.persona import FakePersonaClient, HttpPersonaClient, Lesson
from lucy_api.clients.testing import Answer, FakeHttp, ReadRecorder, problem
from lucy_api.clients.transport import PROFILE_HEADER
from lucy_api.context.feeds import FeedRequest
from lucy_api.packs.notes import NO_LESSON, NotesPack
from lucy_api.packs.service import Capabilities, installed_packs
from lucy_api.permissions.gate import Grant
from lucy_api.prompt.sections import PromptContext, render_all
from lucy_api.sessions.scope import SessionScope

BASE = "http://persona.test"
NOTE = {
    "note_id": "note_9f8e7d6c5b4a",
    "profile": "personal",
    "body": "Put the summary first and the reasoning underneath.",
    "kind": "lesson",
    "source": "assistant",
    "asserted_by": "persona",
    "pinned": True,
    "revision": 1,
    "created_at": "2026-09-24T17:40:00Z",
    "updated_at": "2026-09-24T17:40:00Z",
    "forgotten_at": None,
}
"""Persona-api's `NoteResponse`, as its own schema example gives it."""


class Memory:
    """Enough of the memory service for notes to be ready."""

    async def blocks(self, *, profile: str = "") -> tuple[()]:
        del profile
        return ()


def _hub(
    persona: FakePersonaClient | None = None,
    *,
    mode: str = "auto",
    incognito: bool = False,
    erase: bool = True,
) -> tuple[Capabilities, Any]:
    """`erase` stands in for the person having said yes to unlearning, which asks even in auto."""
    notes = NotesPack("http://memory.test", client=Memory(), persona=persona)
    capabilities = Capabilities((notes,))
    context = capabilities.context_for(
        SessionScope(
            account_id="acct",
            profile="personal",
            session_id="ses",
            permission_mode=mode,
            incognito=incognito,
        )
    )
    if erase:
        context.grants["notes.erase"] = Grant("notes.erase", "allow", "*")
    return capabilities, context


async def _run(capabilities: Capabilities, context: Any, op: str, **inputs: str) -> Any:
    await capabilities.probe(context)
    return await capabilities.execute({"steps": [{"id": "s", "op": op, "input": inputs}]}, context)


async def _data(persona: FakePersonaClient, op: str, **inputs: str) -> dict[str, Any]:
    capabilities, context = _hub(persona)
    result = await _run(capabilities, context, op, **inputs)
    data: dict[str, Any] = result["steps"][0]["data"]
    return data


# --- the operations -----------------------------------------------------------------------


async def test_a_lesson_can_be_kept() -> None:
    """The bug, named: there was no operation that wrote one."""
    persona = FakePersonaClient()
    kept = await _data(persona, "notes.learn", lesson="  Put the summary   first. ")

    assert kept == {"lesson_id": "note_1", "lesson": "Put the summary first.", "revision": 1}
    assert persona.lessons["note_1"].body == "Put the summary first."


async def test_a_lesson_is_reworded_rather_than_kept_twice() -> None:
    persona = FakePersonaClient()
    await persona.learn("Summary first.", profile="personal")
    revised = await _data(persona, "notes.reviseLesson", lesson_id="note_1", lesson="Summary last.")

    assert revised == {"lesson_id": "note_1", "lesson": "Summary last.", "revision": 2}
    assert list(persona.lessons) == ["note_1"]


async def test_a_lesson_can_be_unlearned() -> None:
    persona = FakePersonaClient()
    await persona.learn("Summary first.", profile="personal")
    gone = await _data(persona, "notes.unlearn", lesson_id="note_1")

    assert gone == {"lesson_id": "note_1", "status": "unlearned"}
    assert persona.unlearned == ["note_1"]


@pytest.mark.parametrize(
    ("op", "inputs"),
    [
        ("notes.reviseLesson", {"lesson_id": "note_404", "lesson": "Anything."}),
        ("notes.unlearn", {"lesson_id": "note_404"}),
    ],
)
async def test_an_id_that_is_no_lesson_says_where_the_ids_are(
    op: str, inputs: dict[str, str]
) -> None:
    assert await _data(FakePersonaClient(), op, **inputs) == {
        "status": "not_found",
        "message": NO_LESSON,
    }


async def test_a_full_set_of_lessons_says_which_limit_and_what_to_do() -> None:
    persona = FakePersonaClient(max_pinned=1)
    await persona.learn("Summary first.", profile="personal")
    full = await _data(persona, "notes.learn", lesson="Ask before deleting.")

    assert full["status"] == "full"
    assert full["message"] == (
        "at most 1 pinned notes per persona: revise or unlearn a lesson you no longer need"
    )


@pytest.mark.parametrize("op", ["notes.learn", "notes.reviseLesson"])
async def test_a_lesson_with_no_words_is_refused(op: str) -> None:
    persona = FakePersonaClient()
    await persona.learn("Summary first.", profile="personal")
    refused = await _data(persona, op, lesson_id="note_1", lesson="   ")

    assert refused == {"status": "invalid", "message": "say the lesson, in one sentence"}


@pytest.mark.parametrize(
    ("op", "inputs"),
    [
        ("notes.learn", {"lesson": "Summary first."}),
        ("notes.reviseLesson", {"lesson_id": "note_1", "lesson": "Summary last."}),
        ("notes.unlearn", {"lesson_id": "note_1"}),
    ],
)
async def test_an_incognito_session_keeps_no_lessons(op: str, inputs: dict[str, str]) -> None:
    persona = FakePersonaClient()
    capabilities, context = _hub(persona, incognito=True)
    result = await _run(capabilities, context, op, **inputs)

    assert result["steps"][0]["data"]["status"] == "incognito"
    assert persona.lessons == {}


# --- who may, and where they are offered ---------------------------------------------------


async def test_keeping_or_rewording_asks_like_any_write_and_unlearning_asks_even_in_auto() -> None:
    capabilities, context = _hub(FakePersonaClient(), mode="ask")
    [asked] = (await _run(capabilities, context, "notes.learn", lesson="Summary first."))["issues"]
    assert asked["permission"] == "notes.write"

    capabilities, context = _hub(FakePersonaClient(), mode="auto", erase=False)
    [asked] = (await _run(capabilities, context, "notes.unlearn", lesson_id="note_1"))["issues"]
    assert asked["permission"] == "notes.erase"


def _operations(pack: NotesPack) -> set[str]:
    capabilities = Capabilities((pack,))
    context = capabilities.context_for(
        SessionScope(account_id="acct", profile="personal", session_id="ses")
    )
    return {operation.name for operation in pack.operations(context)}


def test_lessons_are_offered_only_where_there_is_somewhere_to_keep_them() -> None:
    lessons = {"notes.learn", "notes.reviseLesson", "notes.unlearn"}
    assert lessons <= _operations(NotesPack("http://memory.test", persona_base_url=BASE))
    assert not lessons & _operations(NotesPack("http://memory.test"))


def test_this_build_keeps_lessons_in_persona() -> None:
    [notes] = [pack for pack in installed_packs(persona_base_url=BASE) if pack.id == "notes"]
    assert isinstance(notes, NotesPack)
    assert notes.persona_base_url == BASE


# --- the wire --------------------------------------------------------------------------------


async def test_learning_writes_a_pinned_lesson_as_the_assistant() -> None:
    http = FakeHttp(Answer(status_code=201, body=NOTE))
    lesson = await HttpPersonaClient(http, BASE).learn("Summary first.", profile="personal")

    call = http.last
    assert (call.method, call.url, call.audience) == (
        "POST",
        f"{BASE}/v1/personas/personal/notes",
        "persona",
    )
    assert call.json == {
        "body": "Summary first.",
        "kind": "lesson",
        "source": "assistant",
        "pinned": True,
    }
    assert call.headers == {PROFILE_HEADER: "personal"}
    assert lesson == Lesson(id=NOTE["note_id"], body=NOTE["body"], pinned=True, revision=1)


async def test_revising_and_unlearning_address_the_lesson_by_its_id() -> None:
    http = FakeHttp(Answer(body={**NOTE, "revision": 2}), Answer(status_code=204))
    client = HttpPersonaClient(http, BASE)
    revised = await client.revise("note_9f8e7d6c5b4a", "Summary last.", profile="personal")
    await client.unlearn("note_9f8e7d6c5b4a", profile="personal")

    patch, delete = http.calls
    assert (patch.method, patch.url) == (
        "PATCH",
        f"{BASE}/v1/personas/personal/notes/note_9f8e7d6c5b4a",
    )
    assert patch.json == {"body": "Summary last.", "source": "assistant"}
    assert (delete.method, delete.url) == ("DELETE", patch.url)
    assert revised.revision == 2


async def test_the_parser_reads_only_names_persona_api_sends() -> None:
    row = ReadRecorder(NOTE)
    await HttpPersonaClient(FakeHttp(Answer(status_code=201, body=row)), BASE).learn(
        "x", profile="personal"
    )
    assert row.absent == set()


async def test_the_cap_arrives_as_the_limit_it_names() -> None:
    http = FakeHttp(problem(429, detail="at most 10 pinned notes per persona"))
    with pytest.raises(RateLimitedError, match="at most 10 pinned notes per persona"):
        await HttpPersonaClient(http, BASE).learn("Summary first.", profile="personal")


# --- coming back ------------------------------------------------------------------------------


async def test_a_kept_lesson_comes_back_marked_with_the_ref_to_revise_it_by() -> None:
    other = {**NOTE, "note_id": "note_1", "kind": "observation", "body": "Works late."}
    http = FakeHttp(Answer(body={"persona": {}, "fields": [], "notes": [NOTE, other]}))
    [feed] = await PersonaFeeds(http, BASE).fetch(FeedRequest(profile="personal", session_id="s"))

    assert feed.lines == (
        f"lesson: {NOTE['body']} [ref {NOTE['note_id']}]",
        "Works late. [ref note_1]",
    )


def test_the_prompt_says_where_lessons_come_back() -> None:
    prompt = " ".join(" ".join(section.body for section in render_all(PromptContext())).split())
    assert "marked `lesson:`, with the ref you revise or unlearn it by" in prompt
    assert "Once one is written, say so." in prompt


def test_what_a_permission_covers_names_only_what_can_run_here() -> None:
    kept = NotesPack("http://memory.test", persona_base_url=BASE).permissions()
    unkept = NotesPack("http://memory.test").permissions()

    assert kept[0].covers[-2:] == ("notes.learn", "notes.reviseLesson")
    assert kept[1].covers == ("notes.forget", "notes.unlearn")
    assert not {"notes.learn", "notes.unlearn"} & {op for p in unkept for op in p.covers}
