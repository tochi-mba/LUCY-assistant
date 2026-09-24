"""A helper that stops before it finishes says so, says why, and says it can go on.

Seen from the other side of this repository: a helper hit its model's usage limit partway
through its work. The person watching was told at once that it had stopped and why, and
continued it from its own transcript. Lucy's helpers could not be noticed that way, because
the registry recorded a helper that had died as having `succeeded`: the runtime answers a
stopped helper with a result rather than an exception, so that its partial report survives,
and the registry took any result as success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, flakes, plans, speaks
from lucy_api.packs.agents import CONTINUABLE, STOPPED, AgentsPack, _ended, _reopen, _spawn
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.packs.work import _check
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.work.registry import Registry
from lucy_api.work.types import State, WorkError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from lucy_api.packs.context import PackContext

ACCOUNT = "acct_stops"
LIMIT = "You've reached your usage limit. It resets at 5pm."
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


async def test_a_helper_whose_model_ran_out_is_a_failed_ending_that_says_it_can_go_on(
    store: SessionStore,
) -> None:
    """The bug, named: this notice said `succeeded`, with nothing in its detail."""
    provider = ScriptedProvider([plans(READ_DOCS), flakes(LIMIT)])
    context, work, agents = await _parent(store, provider)

    started = await _spawn(work, context, depth=0, objective="Read the helper docs", role="reader")
    ended = await work.wait(str(started["id"]), 30)
    notice = _check(work, context.session_id)["finished"][0]

    assert ended.state is State.failed
    assert notice["state"] == "failed"
    assert notice["detail"].startswith(CONTINUABLE)
    assert "usage limit" in notice["detail"]
    report = ended.payload
    assert isinstance(report, dict)
    assert report["resumable"] is True
    assert report["termination"] == "error_during_execution"
    assert (await agents.get(ACCOUNT, str(started["id"])))["status"] == "failed"


async def test_a_stopped_helper_is_continued_from_where_it_got_to(store: SessionStore) -> None:
    """What the notice offers works: the continuation reads the first run's transcript."""
    provider = ScriptedProvider([plans(READ_DOCS), flakes(LIMIT), speaks("Read them; all done.")])
    context, work, _agents = await _parent(store, provider)
    started = await _spawn(work, context, depth=0, objective="Read the helper docs", role="reader")
    await work.wait(str(started["id"]), 30)

    reopened = await _reopen(work, context, depth=0, agent_id=str(started["id"]))
    finished = await work.wait(str(reopened["id"]), 30)

    assert finished.state is State.succeeded
    assert isinstance(finished.payload, dict)
    assert finished.payload["summary"] == "Read them; all done."
    assert finished.payload["resumable"] is False
    handed = [str(message.content) for message in provider.requests[-1].messages]
    assert any("A helper is work" in text for text in handed), "the first run's read is lost"
    assert any(f"Continue from helper {started['id']}" in text for text in handed)


def test_a_helper_that_finished_is_handed_back_as_it_was() -> None:
    result = {"status": "ok", "summary": "done"}
    assert _ended(result) is result


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (
            {"status": "failed", "resumable": True, "summary": "the helper stopped (OSError)"},
            f"{CONTINUABLE}: the helper stopped (OSError)",
        ),
        (
            {"status": "failed", "resumable": False, "summary": " waiting on a person "},
            f"{STOPPED}: waiting on a person",
        ),
        ({"status": "failed", "summary": ""}, STOPPED),
    ],
)
def test_a_helper_that_did_not_finish_is_raised_with_its_report(
    result: dict[str, Any], detail: str
) -> None:
    with pytest.raises(WorkError) as stopped:
        _ended(result)
    assert str(stopped.value) == detail
    assert stopped.value.payload is result


async def test_a_helper_that_finished_is_not_offered_for_continuing(store: SessionStore) -> None:
    context, _work, _agents = await _parent(store, ScriptedProvider([speaks("found it")]))
    child = context.child
    assert child is not None
    result = await child.run(context, objective="Find it", role="finder")
    assert result["status"] == "ok"
    assert result["resumable"] is False


def test_work_that_fails_in_its_own_words_without_a_report_has_no_payload() -> None:
    assert WorkError("five checks failed").payload is None
