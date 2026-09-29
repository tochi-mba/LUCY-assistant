"""What a helper did can be read, however it ended, without starting it again.

A helper writes each item as it happens, so its work survives the moment it breaks. But the
only way Lucy had to see inside a stopped helper was `agents.reopen`, which starts a new run to
do it: there was no way to read what a broken helper had found and use it, or to look at a
running one. The person could, through the API; the model could not.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from test_helper_stops import LIMIT, READ_DOCS, _parent

from lucy_api.agents.runtime import NO_SUCH_HELPER
from lucy_api.model.scripted import ScriptedProvider, flakes, plans, speaks
from lucy_api.model.types import Reply, Request
from lucy_api.packs.agents import AgentsPack, _read, _reopen, _spawn
from lucy_api.packs.work import _cancel
from lucy_api.prompt.sections import PromptContext, render_all
from lucy_api.sessions.models import CreateSession
from lucy_api.work.types import State

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore


def _texts(read: dict[str, Any]) -> list[str]:
    return [str(item["text"]) for item in read["items"]]


async def test_a_helper_that_broke_can_be_read_without_continuing_it(
    sessions_store: SessionStore,
) -> None:
    """The bug, named: what it had read before its model ran out could not be seen."""
    provider = ScriptedProvider([plans(READ_DOCS), flakes(LIMIT)])
    context, work, agents = await _parent(sessions_store, provider)
    started = await _spawn(work, context, depth=0, objective="Read the helper docs", role="reader")
    await work.wait(str(started["id"]), 30)

    read = await _read(context, str(started["id"]))

    assert (read["state"], read["role"], read["objective"]) == (
        "failed",
        "reader",
        "Read the helper docs",
    )
    brief, step = _texts(read)
    assert brief.startswith("You are a helper named reader.")
    assert step.startswith("[step docs: help.docs -- ok]")
    assert "A helper is work" in step
    assert (read["count"], read["runs"]) == (2, 1)
    assert len(await agents.for_session(context.account_id, context.session_id)) == 1


async def test_a_helper_stopped_mid_run_keeps_what_it_had_done(
    sessions_store: SessionStore,
) -> None:
    reached = asyncio.Event()

    class Hangs(ScriptedProvider):
        async def complete(self, request: Request) -> Reply:
            if self.calls == 1:
                reached.set()
                await asyncio.Event().wait()
            return await super().complete(request)

    context, work, _agents = await _parent(sessions_store, Hangs([plans(READ_DOCS)]))
    started = await _spawn(work, context, depth=0, objective="Read the helper docs", role="reader")
    await reached.wait()

    running = await _read(context, str(started["id"]))
    _cancel(work, str(started["id"]))
    ended = await work.wait(str(started["id"]), 30)
    stopped = await _read(context, str(started["id"]))

    assert running["state"] == "running"
    assert ended.state is State.cancelled
    assert stopped["state"] == "interrupted"
    assert _texts(stopped) == _texts(running)
    assert _texts(stopped)[1].startswith("[step docs: help.docs -- ok]")


async def test_a_continued_helper_is_read_with_the_runs_before_it(
    sessions_store: SessionStore,
) -> None:
    provider = ScriptedProvider([plans(READ_DOCS), flakes(LIMIT), speaks("Read them; all done.")])
    context, work, _agents = await _parent(sessions_store, provider)
    first = await _spawn(work, context, depth=0, objective="Read the helper docs", role="reader")
    await work.wait(str(first["id"]), 30)
    second = await _reopen(work, context, depth=0, agent_id=str(first["id"]))
    await work.wait(str(second["id"]), 30)

    read = await _read(context, str(second["id"]))

    assert (read["state"], read["runs"]) == ("completed", 2)
    texts = _texts(read)
    assert texts[1].startswith("[step docs: help.docs -- ok]"), "the first run's step"
    assert texts[-1] == "Read them; all done."


async def test_only_this_conversation_s_helpers_can_be_read(sessions_store: SessionStore) -> None:
    provider = ScriptedProvider([speaks("Found it."), speaks("unused")])
    context, work, _agents = await _parent(sessions_store, provider)
    started = await _spawn(work, context, depth=0, objective="Find it", role="reader")
    await work.wait(str(started["id"]), 30)
    other = await sessions_store.create(
        context.account_id, CreateSession(model="scripted:demo"), "other-session"
    )
    elsewhere = replace(context, session_id=str(other["id"]))

    missing = {"status": "not_found", "message": NO_SUCH_HELPER}
    assert await _read(elsewhere, str(started["id"])) == missing
    assert await _read(context, "agt_nobody") == missing


async def test_a_turn_with_no_helper_runtime_says_so(sessions_store: SessionStore) -> None:
    context, _work, _agents = await _parent(sessions_store, ScriptedProvider([]))
    context.child = None
    assert (await _read(context, "agt_any"))["status"] == "not_configured"


async def test_a_helper_can_read_its_own_helpers_too(sessions_store: SessionStore) -> None:
    context, _work, _agents = await _parent(sessions_store, ScriptedProvider([]))
    as_parent = {operation.name for operation in AgentsPack().operations(context)}
    as_helper = {
        operation.name
        for operation in AgentsPack().operations(replace(context, agent_id="agt_helper"))
    }

    assert "agents.read" in as_parent
    assert "agents.read" in as_helper


def test_the_model_is_told_it_can_read_and_stop_a_helper() -> None:
    prompt = " ".join(" ".join(section.body for section in render_all(PromptContext())).split())
    assert "What it did before it failed is not lost" in prompt
    assert "stopping it keeps what it had found" in prompt
