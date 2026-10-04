"""`lucy.helper_model`: helpers on their own model, the conversation on the person's.

Five parallel reviewers on the best model is expensive, so a person may name a cheaper one
for helpers. It never changes the conversation's model, and a helper model that cannot run
here, or is down, falls back to the conversation's and says so.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from lucy_api.agents.runtime import ChildRuntime, _helper_spec
from lucy_api.agents.store import AgentStore
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, flakes, speaks
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from lucy_api.packs.context import PackContext

ACCOUNT = "acct_helpers"
CONVERSATION = "scripted:best"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


def _runtime(
    store: SessionStore, conversation: ScriptedProvider, cheap: ScriptedProvider
) -> tuple[ChildRuntime, Capabilities]:
    capabilities = Capabilities((HelpPack(), AgentsPack()))
    models = ModelRegistry({"scripted": lambda _model: conversation, "cheap": lambda _m: cheap})
    child = ChildRuntime(store, AgentStore(store), models, capabilities)
    capabilities.child = child
    return child, capabilities


async def _parent(
    store: SessionStore, capabilities: Capabilities, helper_model: str
) -> PackContext:
    created = await store.create(ACCOUNT, CreateSession(model=CONVERSATION), "key")
    scope = SessionScope(account_id=ACCOUNT, profile="personal", session_id=str(created["id"]))
    context = capabilities.context_for(scope)
    context.policy = replace(context.policy, helper_model=helper_model)
    return context


async def test_a_named_helper_model_runs_the_helper_and_leaves_the_conversation_alone(
    store: SessionStore,
) -> None:
    """The helper's request goes to the helper model, by its own id; the session still
    names the person's model and the conversation's provider is never asked."""
    conversation = ScriptedProvider([])
    cheap = ScriptedProvider([speaks("Paris in June.")])
    child, capabilities = _runtime(store, conversation, cheap)
    parent = await _parent(store, capabilities, "cheap:mini")

    result = await child.run(parent, objective="When is the tour?", role="researcher")

    assert result["status"] == "ok"
    assert result["summary"] == "Paris in June."
    assert result["notice"] == ""
    assert [request.model for request in cheap.requests] == ["mini"]
    assert conversation.calls == 0
    assert (await store.get(ACCOUNT, parent.session_id))["model"] == CONVERSATION


async def test_a_helper_model_this_hub_cannot_run_falls_back_and_says_so(
    store: SessionStore,
) -> None:
    """A provider with nothing configured is not a failed helper: it runs on the
    conversation's model, and its report names the swap and the registry's reason."""
    conversation = ScriptedProvider([speaks("done")])
    child, capabilities = _runtime(store, conversation, ScriptedProvider([]))
    parent = await _parent(store, capabilities, "anthropic:claude-haiku-4-5")

    result = await child.run(parent, objective="Look it up", role="helper")

    assert result["status"] == "ok"
    assert conversation.calls == 1
    assert result["notice"].startswith(
        "the helper model anthropic:claude-haiku-4-5 cannot run here, so this helper ran on "
        f"the conversation's model, {CONVERSATION}: "
    )
    assert "lucy models connect anthropic" in result["notice"]


async def test_a_helper_model_that_is_down_is_answered_by_the_conversation_s_model(
    store: SessionStore,
) -> None:
    """Unavailable mid-run, the helper model gets the same one retry a main turn's
    fallback_model does, on the conversation's model, and the reply says who answered."""
    conversation = ScriptedProvider([speaks("covered")])
    cheap = ScriptedProvider([flakes("overloaded")])
    child, capabilities = _runtime(store, conversation, cheap)
    parent = await _parent(store, capabilities, "cheap:mini")

    result = await child.run(parent, objective="Look it up", role="helper")

    assert result["status"] == "ok"
    assert cheap.calls == 1
    assert conversation.calls == 1
    assert conversation.requests[0].model == ""
    assert result["summary"].startswith(f"(Answered by {CONVERSATION} because")
    assert result["summary"].endswith("covered")


def test_naming_the_conversation_s_own_model_is_the_same_as_naming_none() -> None:
    """No fallback to itself and nothing to say: the default path, unchanged."""
    models = ModelRegistry({"scripted": lambda _model: ScriptedProvider([])})

    assert _helper_spec(models, "", CONVERSATION) == (CONVERSATION, "")
    assert _helper_spec(models, CONVERSATION, CONVERSATION) == (CONVERSATION, "")
    assert _helper_spec(models, "scripted:small", CONVERSATION) == ("scripted:small", "")
