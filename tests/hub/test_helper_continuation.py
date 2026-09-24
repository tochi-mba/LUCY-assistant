"""Continuing a helper carries on from all of it, once, and a stopped one stays findable.

A continuation read the items of the one run it named and no others, so a helper stopped
twice came back knowing only its second attempt -- while its brief told it "its earlier
items are in this transcript". Nothing stopped one helper being continued twice, which runs
the same remaining work twice, differently. And a stopped helper could be found only in the
notice that announced it: `agents.list` showed what was running and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from lucy_api.agents.runtime import ChildRuntime, _earlier_runs
from lucy_api.agents.store import AgentStore
from lucy_api.agents.types import RESTARTED
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, flakes, plans, speaks
from lucy_api.packs.agents import MAX_STOPPED_LISTED, AgentsPack, _list, _reopen, _spawn
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.work.registry import Registry
from lucy_api.work.types import State

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from lucy_api.packs.context import PackContext

ACCOUNT = "acct_continue"
LIMIT = "You've reached your usage limit."
READ_DOCS = {"steps": [{"id": "docs", "op": "help.docs", "input": {"topic": "agents"}}]}


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def _parent(
    store: SessionStore, provider: ScriptedProvider
) -> tuple[PackContext, Registry, AgentStore]:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "session-key")
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities((HelpPack(), AgentsPack()), work=work)
    agents = AgentStore(store)
    capabilities.child = ChildRuntime(
        store, agents, ModelRegistry({"scripted": lambda _model: provider}), capabilities
    )
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=str(created["id"]))
    return capabilities.context_for(scope), work, agents


async def test_a_helper_continued_twice_still_has_its_first_run(store: SessionStore) -> None:
    """The bug, named: the third run saw the second run's items and not the first's."""
    provider = ScriptedProvider(
        [plans(READ_DOCS), flakes(LIMIT), flakes(LIMIT), speaks("Finished reading.")]
    )
    context, work, _agents = await _parent(store, provider)
    first = await _spawn(work, context, depth=0, objective="Read the helper docs", role="reader")
    await work.wait(str(first["id"]), 30)
    second = await _reopen(work, context, depth=0, agent_id=str(first["id"]))
    assert (await work.wait(str(second["id"]), 30)).state is State.failed

    third = await _reopen(work, context, depth=0, agent_id=str(second["id"]))
    finished = await work.wait(str(third["id"]), 30)

    assert finished.state is State.succeeded
    handed = [str(message.content) for message in provider.requests[-1].messages]
    assert any("A helper is work" in text for text in handed), "the first run's read is lost"
    assert sum("You are a helper named reader." in text for text in handed) == 3


async def test_a_helper_already_continued_is_not_continued_again(store: SessionStore) -> None:
    provider = ScriptedProvider([flakes(LIMIT), speaks("done")])
    context, work, _agents = await _parent(store, provider)
    first = await _spawn(work, context, depth=0, objective="Read it", role="reader")
    await work.wait(str(first["id"]), 30)
    second = await _reopen(work, context, depth=0, agent_id=str(first["id"]))
    await work.wait(str(second["id"]), 30)

    again = await _reopen(work, context, depth=0, agent_id=str(first["id"]))

    assert again["status"] == "continued"
    assert str(second["id"]) in again["message"]


async def test_a_stopped_helper_is_listed_until_it_is_continued(store: SessionStore) -> None:
    provider = ScriptedProvider([flakes(LIMIT), speaks("done")])
    context, work, _agents = await _parent(store, provider)
    first = await _spawn(work, context, depth=0, objective="Read it", role="reader")
    await work.wait(str(first["id"]), 30)

    listed = await _list(work, context)

    assert listed["count"] == 0
    assert listed["stopped_count"] == 1
    [line] = listed["stopped"]
    assert line["id"] == first["id"]
    assert line["objective"] == "Read it"
    assert "usage limit" in line["why"]
    assert line["resumable"] is True
    assert "agents.reopen" in listed["advice"]

    second = await _reopen(work, context, depth=0, agent_id=str(first["id"]))
    await work.wait(str(second["id"]), 30)
    assert "stopped" not in await _list(work, context)


async def test_what_is_listed_is_this_caller_s_helpers_that_stopped_on_their_own(
    store: SessionStore,
) -> None:
    context, work, agents = await _parent(store, ScriptedProvider([speaks("x")]))
    session = context.session_id

    async def helper(status: str, **finish: object) -> str:
        agent_id = await agents.insert(ACCOUNT, session, role="r", objective=status, depth=1)
        await agents.finish(ACCOUNT, agent_id, status=status, **finish)
        return agent_id

    restarted = await helper("interrupted", interrupted_reason="process_restarted")
    await helper("interrupted", interrupted_reason="cancelled")
    await helper("completed", result={"summary": "all done"})
    silent = await helper("failed")
    wordless = await helper("failed", result={"resumable": True})
    nested = await agents.insert(
        ACCOUNT, session, role="r", objective="nested", depth=2, parent_agent_id=restarted
    )
    await agents.finish(ACCOUNT, nested, status="failed", result={"summary": "x"})

    lines = {line["id"]: line for line in (await _list(work, context))["stopped"]}

    assert set(lines) == {restarted, silent, wordless}
    assert (lines[restarted]["why"], lines[restarted]["resumable"]) == (RESTARTED, True)
    assert (lines[silent]["why"], lines[silent]["resumable"]) == ("failed", False)
    assert (lines[wordless]["why"], lines[wordless]["resumable"]) == ("failed", True)


async def test_the_stopped_list_is_the_newest_few_and_says_how_many(store: SessionStore) -> None:
    context, work, agents = await _parent(store, ScriptedProvider([speaks("x")]))
    ids = []
    for index in range(MAX_STOPPED_LISTED + 2):
        agent_id = await agents.insert(
            ACCOUNT, context.session_id, role="r", objective=f"job {index}", depth=1
        )
        await agents.finish(ACCOUNT, agent_id, status="failed", result={"summary": "x"})
        ids.append(agent_id)

    listed = await _list(work, context)

    assert listed["stopped_count"] == MAX_STOPPED_LISTED + 2
    assert len(listed["stopped"]) == MAX_STOPPED_LISTED
    assert listed["stopped"][-1]["objective"] == f"job {MAX_STOPPED_LISTED + 1}"


def test_a_chain_that_loops_back_on_itself_ends() -> None:
    rows = [
        {"id": "agt_a", "delegation": {"resume_from": "agt_b"}},
        {"id": "agt_b", "delegation": {"resume_from": "agt_a"}},
        {"id": "agt_c", "delegation": None},
    ]
    assert _earlier_runs(rows, "agt_a") == {"agt_a", "agt_b"}
    assert _earlier_runs(rows, "agt_c") == {"agt_c"}
    assert _earlier_runs(rows, "") == frozenset()
