"""The context line counts what every request carries, not only the transcript.

Read in the requests the hub actually sent: the live block said `11 of 200,000 tokens (0% used)`
on a request carrying some 17,000 -- the system prompt and the plan schema, sent every round and
counted nowhere. Warnings and compaction read the same number, so a small window could be full
of prompt while the model was told it was empty.
"""

from __future__ import annotations

import json
from dataclasses import replace
from math import ceil
from typing import TYPE_CHECKING

from conftest import bearer

from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore
from lucy_api.context.tokens import CHARS_PER_TOKEN
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.model.types import Message
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.prompt.sections import PromptContext, render_all
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.turns import submit_messages
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.prompt import SessionView, projected_rows, schema_tokens, system_and_messages
from lucy_api.turn.supervisor import TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import Sequence

    from httpx import AsyncClient

    from lucy_api.sessions.sql_store import SessionStore

ACCOUNT = "acct_context_used"
SCHEMA = 5_000
SESSION = {"profile": "personal", "title": "", "permission_mode": "ask", "incognito": 0}


def _fixed(capabilities: tuple[str, ...]) -> int:
    return sum(section.tokens for section in render_all(PromptContext(capabilities=capabilities)))


def _said_used(system: str, messages: Sequence[Message]) -> int:
    """The number on the live block's context line."""
    whole = system + "\n" + "\n".join(message.content for message in messages)
    line = next(row for row in whole.splitlines() if row.startswith("context") and " of " in row)
    return int(line.split(" of ", maxsplit=1)[0].split()[-1].replace(",", ""))


async def test_an_empty_conversation_is_not_an_empty_request() -> None:
    """The bug, named: this line said the transcript's size and nothing else."""
    view = SessionView(
        session_id="ses", items=[], capabilities=("help",), session=SESSION, schema_tokens=SCHEMA
    )
    used = _said_used(*await system_and_messages(view))
    assert used == _fixed(("help",)) + SCHEMA


def test_the_schema_is_counted_as_it_goes_over_the_wire() -> None:
    schema = {"type": "object", "properties": {"say": {"type": "string"}}}
    compact = json.dumps(schema, separators=(",", ":"))
    assert schema_tokens(schema) == ceil(len(compact) / CHARS_PER_TOKEN)


def test_a_small_window_full_of_prompt_warns_and_compacts() -> None:
    """Warnings and compaction read the same number the model is told."""
    view = SessionView(
        session_id="ses",
        items=[
            {"id": "itm_1", "seq": 1, "role": "user", "content": "hi", "type": "message"},
        ],
        window=8_000,
        schema_tokens=SCHEMA,
    )
    _rows, reclaimed = projected_rows(view)
    assert reclaimed.should_compact
    assert any(notice.endswith("of 8,000 tokens") for notice in reclaimed.notices)


async def test_a_turn_counts_the_schema_it_sends(sessions_store: SessionStore) -> None:
    created = await sessions_store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    session_id = str(created["id"])
    await submit_messages(
        sessions_store,
        ACCOUNT,
        session_id,
        [{"type": "input.message", "content": "hello"}],
        "input-key",
    )
    provider = ScriptedProvider([speaks("hello")])
    running = TurnSupervisor(
        sessions_store,
        ModelRegistry({"scripted": lambda _: provider}),
        EventEmitter(SqlEventLog(sessions_store), _Snapshot()),
        capabilities=Capabilities((HelpPack(),)),
    )
    running.wake()
    await running.join()
    await running.aclose()

    [request] = provider.requests
    assert request.plan_schema
    assert _said_used(request.system, request.messages) > schema_tokens(request.plan_schema)


async def test_a_helper_counts_the_schema_it_sends(sessions_store: SessionStore) -> None:
    created = await sessions_store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    session_id = str(created["id"])
    provider = ScriptedProvider([speaks("Paris in June.")])
    capabilities = Capabilities((HelpPack(), AgentsPack()))
    child = ChildRuntime(
        sessions_store,
        AgentStore(sessions_store),
        ModelRegistry({"scripted": lambda _model: provider}),
        capabilities,
    )
    capabilities.child = child
    parent = capabilities.context_for(
        SessionScope(account_id=ACCOUNT, profile="personal", session_id=session_id)
    )
    await child.run(parent, objective="When is the tour?", role="researcher")

    [request] = provider.requests
    assert request.plan_schema
    assert _said_used(request.system, request.messages) > schema_tokens(request.plan_schema)


async def test_the_context_preview_says_the_same(client: AsyncClient) -> None:
    """`GET /context` is "the exact prompt this session would send": it counts the schema too."""
    created = await client.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "used-session"}
    )
    context = await client.get(f"/v1/sessions/{created.json()['id']}/context", headers=bearer())

    document = context.json()
    used = _said_used(document["prompt"], ())
    assert used > document["bands"]["system"]


class _Snapshot:
    async def snapshot(self, session_id: str) -> dict[str, str]:
        return {"session_id": session_id}


def _long_conversation(turns: int) -> list[dict[str, object]]:
    body = "a sentence long enough to count for something in the window. " * 40
    return [
        {
            "id": f"itm_{seq}",
            "seq": seq,
            "role": "user" if seq % 2 else "assistant",
            "content": body,
            "turn_id": f"trn_{(seq + 1) // 2}",
            "type": "message",
        }
        for seq in range(1, turns * 2 + 1)
    ]


def test_a_compaction_lowers_the_figure_and_does_not_run_again() -> None:
    """The bug, named: the figure counted every turn the transcript held, summarised or
    not. A compaction never lowered it, so the window read as full as before, the model
    was told so, and compaction ran again on every turn after the first."""
    items = _long_conversation(10)
    full = SessionView(session_id="ses", items=items, window=12_000, compact_at_percent=72)
    _rows, before = projected_rows(full)
    assert before.should_compact

    compacted = replace(
        full,
        compactions=[
            {"seq": 1, "summary": "Opening.", "covers_from": 1, "covers_to": 16, "active": 1}
        ],
    )
    rows, after = projected_rows(compacted)

    assert after.used < before.used // 2
    assert not after.should_compact, "a compaction that just ran does not ask to run again"
    assert [row["id"] for row in rows] == [row["id"] for row in items], (
        "covered rows stay, so the history band can still find what its summary stands for"
    )


def test_covered_tool_results_are_not_offered_for_reclamation() -> None:
    items = [
        *_long_conversation(3),
        {
            "id": "itm_t",
            "seq": 7,
            "role": "tool",
            "content": "{}",
            "turn_id": "trn_4",
            "type": "tool_result",
        },
        {
            "id": "itm_u",
            "seq": 8,
            "role": "user",
            "content": "next",
            "turn_id": "trn_5",
            "type": "message",
        },
    ]
    covered = SessionView(
        session_id="ses",
        items=items,
        compactions=[
            {"seq": 1, "summary": "All of it.", "covers_from": 1, "covers_to": 7, "active": 1}
        ],
    )
    _rows, reclaimed = projected_rows(covered)
    assert reclaimed.reclaimable == 0
    _rows, open_ = projected_rows(replace(covered, compactions=[]))
    assert open_.reclaimable == 1
