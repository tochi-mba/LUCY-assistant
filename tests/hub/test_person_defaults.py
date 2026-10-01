"""A person's command timeout, output cap, recall size and trust floor are what a turn uses.

The bug, named: `environments.command_timeout_seconds`, `environments.max_output_bytes`,
`memory.retrieval_limit` and `memory.retrieval_trust_floor` could be set and read back, and
changed nothing. A command that named no timeout always got sixty seconds, a recall always
brought back ten, and a person who chose "only what I told you" still had inferences about
them retrieved.
"""

from __future__ import annotations

from typing import Any

import pytest
from settings_client import SettingsRefused

from lucy_api.clients.environments import (
    DEFAULT_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    Environment,
    FakeEnvironmentsClient,
)
from lucy_api.clients.testing import Answer, FakeHttp
from lucy_api.core.container import NAMESPACES_READ, SIBLING_NAMESPACES
from lucy_api.packs.help import HelpPack
from lucy_api.packs.notes import FLOOR_NOT_KNOWN, NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.packs.workspace import MAX_TIMEOUT_MS, WorkspacePack
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.settings.defaults import (
    FLOOR_UNKNOWN,
    MEMORY_NAMESPACE,
    WORKSPACE_NAMESPACE,
    pack_defaults,
)

# --------------------------------------------------------------------------------------
# What the two namespaces supply
# --------------------------------------------------------------------------------------


class _Refusing(dict[str, Any]):
    """A resolved namespace in an outage: one key cannot be read and must not be guessed."""

    def __init__(self, refused: str, **values: Any) -> None:
        super().__init__(values)
        self._refused = refused

    def get(self, key: str, default: Any = None) -> Any:
        if key == self._refused:
            raise SettingsRefused("memory", key)
        return super().get(key, default)


def test_the_hub_reads_and_is_granted_both_namespaces() -> None:
    assert {WORKSPACE_NAMESPACE, MEMORY_NAMESPACE} <= set(SIBLING_NAMESPACES)
    assert set(SIBLING_NAMESPACES) < set(NAMESPACES_READ)


def test_an_unavailable_memory_namespace_never_restores_inferred_retrieval() -> None:
    """The bug, named: a namespace outage silently removed the person's trust floor."""
    assert pack_defaults({MEMORY_NAMESPACE: None}) == {"notes.trust_floor": FLOOR_UNKNOWN}


def test_a_command_timeout_and_an_output_cap_become_workspace_defaults() -> None:
    chosen = {WORKSPACE_NAMESPACE: {"command_timeout_seconds": 300, "max_output_bytes": 8_192}}

    assert pack_defaults(chosen) == {
        "workspace.timeout_ms": 300_000,
        "workspace.output_bytes": 8_192,
    }


@pytest.mark.parametrize(
    "values",
    [{}, {"command_timeout_seconds": "long", "max_output_bytes": True}],
    ids=["unset", "wrong-kinds"],
)
def test_a_workspace_value_that_is_not_a_number_supplies_nothing(values: dict[str, Any]) -> None:
    assert pack_defaults({WORKSPACE_NAMESPACE: values}) == {}


def test_a_nonsense_number_is_still_at_least_one() -> None:
    chosen = {WORKSPACE_NAMESPACE: {"command_timeout_seconds": 0, "max_output_bytes": -4}}

    assert pack_defaults(chosen) == {"workspace.timeout_ms": 1_000, "workspace.output_bytes": 1}


@pytest.mark.parametrize(("chosen", "limit"), [(0, 0), (5, 5), (12, 12), (100, 20), (-3, 0)])
def test_a_recall_size_is_held_between_none_and_twenty(chosen: int, limit: int) -> None:
    defaults = pack_defaults({MEMORY_NAMESPACE: {"retrieval_limit": chosen}})

    assert defaults["notes.limit"] == limit


@pytest.mark.parametrize("floor", ["stated", "observed", "inferred"])
def test_a_trust_floor_is_passed_through(floor: str) -> None:
    defaults = pack_defaults({MEMORY_NAMESPACE: {"retrieval_trust_floor": floor}})

    assert defaults == {"notes.trust_floor": floor}


def test_memory_with_nothing_chosen_is_the_widest_floor_and_no_limit() -> None:
    assert pack_defaults({MEMORY_NAMESPACE: {}}) == {"notes.trust_floor": "inferred"}


def test_a_floor_that_cannot_be_read_is_unknown_and_never_guessed() -> None:
    refused = _Refusing("retrieval_trust_floor", retrieval_limit=4)

    assert pack_defaults({MEMORY_NAMESPACE: refused}) == {
        "notes.trust_floor": FLOOR_UNKNOWN,
        "notes.limit": 4,
    }


@pytest.mark.parametrize("floor", ["everything", 3, None])
def test_a_floor_that_is_not_one_of_the_three_is_unknown(floor: object) -> None:
    defaults = pack_defaults({MEMORY_NAMESPACE: {"retrieval_trust_floor": floor}})

    assert defaults["notes.trust_floor"] == FLOOR_UNKNOWN


# --------------------------------------------------------------------------------------
# A command that does not say
# --------------------------------------------------------------------------------------


async def ran(defaults: dict[str, object], **said: Any) -> tuple[int, int]:
    """The timeout and the output cap one `workspace.run` reached the sandbox with."""
    fake = FakeEnvironmentsClient()
    fake.seed(Environment("env-1", "Conversation", profile="personal"))
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    context.defaults = defaults
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "run", "op": "workspace.run", "input": {"command": "build", **said}}]},
        context,
    )
    assert not result["issues"]
    _environment, _command, timeout_ms, output_bytes = fake.ran[0]
    return timeout_ms, output_bytes


async def test_with_nothing_chosen_a_command_gets_this_services_defaults() -> None:
    assert await ran({}) == (DEFAULT_TIMEOUT_MS, DEFAULT_OUTPUT_BYTES)


async def test_a_command_that_names_no_timeout_gets_the_persons() -> None:
    timeout_ms, _bytes = await ran({"workspace.timeout_ms": 5_000})

    assert timeout_ms == 5_000


async def test_a_command_that_names_a_timeout_keeps_it() -> None:
    timeout_ms, _bytes = await ran({"workspace.timeout_ms": 5_000}, timeout_ms=90_000)

    assert timeout_ms == 90_000


async def test_the_persons_timeout_cannot_pass_the_most_a_command_may_have() -> None:
    timeout_ms, _bytes = await ran({"workspace.timeout_ms": 3_600_000})

    assert timeout_ms == MAX_TIMEOUT_MS


async def test_an_output_cap_narrows_and_never_widens() -> None:
    _timeout, narrowed = await ran({"workspace.output_bytes": 4_096})
    _timeout, widened = await ran({"workspace.output_bytes": 1_048_576})

    assert narrowed == 4_096
    assert widened == DEFAULT_OUTPUT_BYTES


async def test_a_default_of_the_wrong_kind_is_not_used() -> None:
    wrong = {"workspace.timeout_ms": "soon", "workspace.output_bytes": "lots"}

    assert await ran(wrong) == (DEFAULT_TIMEOUT_MS, DEFAULT_OUTPUT_BYTES)


# --------------------------------------------------------------------------------------
# A recall
# --------------------------------------------------------------------------------------


def memory(memory_id: str, trust: str) -> dict[str, str]:
    return {"id": memory_id, "title": memory_id, "body": "…", "kind": "fact", "trust": trust}


FOUND = Answer(
    body={
        "data": [
            memory("mem_said", "stated"),
            memory("mem_seen", "observed"),
            memory("mem_guessed", "inferred"),
        ]
    }
)


async def search(defaults: dict[str, object], **said: Any) -> tuple[dict[str, Any], FakeHttp]:
    http = FakeHttp(Answer(body={"data": []}), FOUND)
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"), http=http
    )
    context.defaults = defaults
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "q", "op": "notes.search", "input": {"query": "tea", **said}}]},
        context,
    )
    return result["steps"][0], http


def ids(step: dict[str, Any]) -> list[str]:
    return [item["id"] for item in step["items"]]


async def test_with_no_floor_resolved_a_recall_is_what_it_always_was() -> None:
    step, http = await search({})

    assert ids(step) == ["mem_said", "mem_seen", "mem_guessed"]
    assert not step.get("notices")
    assert http.last.params is not None
    assert http.last.params["limit"] == 10


async def test_the_widest_floor_leaves_nothing_out() -> None:
    step, _http = await search({"notes.trust_floor": "inferred"})

    assert ids(step) == ["mem_said", "mem_seen", "mem_guessed"]
    assert not step.get("notices")


async def test_a_floor_keeps_itself_and_better_and_says_how_many_it_left_out() -> None:
    observed, _http = await search({"notes.trust_floor": "observed"})
    stated, _http = await search({"notes.trust_floor": "stated"})

    assert ids(observed) == ["mem_said", "mem_seen"]
    assert observed["notices"] == (
        "1 more matched and was left out: this person's settings use only what is observed "
        "or better.",
    )
    assert ids(stated) == ["mem_said"]
    assert stated["notices"] == (
        "2 more matched and were left out: this person's settings use only what is stated "
        "or better.",
    )


async def test_an_unknown_floor_retrieves_nothing_and_says_why() -> None:
    step, _http = await search({"notes.trust_floor": FLOOR_UNKNOWN})

    assert step["items"] == ()
    assert step["notices"] == (FLOOR_NOT_KNOWN,)


async def test_a_recall_that_names_no_size_takes_the_persons() -> None:
    _step, chosen = await search({"notes.limit": 4})
    _step, asked = await search({"notes.limit": 4}, limit=7)
    _step, none = await search({"notes.limit": 0})

    assert chosen.last.params is not None
    assert chosen.last.params["limit"] == 4
    assert asked.last.params is not None
    assert asked.last.params["limit"] == 7
    # Zero is "bring nothing in until asked", and a search is the asking.
    assert none.last.params is not None
    assert none.last.params["limit"] == 1


async def about_me(defaults: dict[str, object]) -> tuple[dict[str, Any], FakeHttp]:
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": [{"label": "human", "body": "a person"}]}),
        FOUND,
    )
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"), http=http
    )
    context.defaults = defaults
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "me", "op": "notes.aboutMe", "input": {}}]}, context
    )
    return result["steps"][0], http


async def test_what_lucy_knows_about_me_is_held_to_the_same_floor_and_size() -> None:
    step, http = await about_me({"notes.trust_floor": "stated", "notes.limit": 6})

    assert [fact["id"] for fact in step["data"]["facts"]] == ["mem_said"]
    assert http.last.params is not None
    assert http.last.params["limit"] == 6


async def test_a_recall_size_of_none_brings_no_facts_and_asks_for_none() -> None:
    step, http = await about_me({"notes.limit": 0})

    assert step["data"]["facts"] == []
    assert step["data"]["blocks"] == [{"label": "human", "body": "a person"}]
    assert len(http.calls) == 2, "the probe and the blocks; no search was made"
