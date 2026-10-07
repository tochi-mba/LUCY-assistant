"""The plan schema says each thing once.

Read in the requests the hub actually sent: a 45,109-character plan schema every round, of which
17.7K were two definitions copied onto all 52 operations -- `show_from` with a 193-character
explanation, and `id` with "Short name for this step's result; later steps reference it as
$id." The prompt's tools section already explains both, once.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from lucy_api.packs.help import HelpPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.prompt.sections import PromptContext, render_all
from lucy_api.sessions.scope import SessionScope


def _schema() -> dict[str, Any]:
    async def build() -> dict[str, Any]:
        capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
        context = capabilities.context_for(
            SessionScope(account_id="acct", profile="personal", session_id="ses")
        )
        return capabilities.plan_schema(await capabilities.probe(context), "ses", context)

    return asyncio.run(build())


def _variants() -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = _schema()["properties"]["steps"]["items"]["anyOf"]
    return variants


def test_no_operation_repeats_what_an_id_is() -> None:
    """The bug, named: fifty copies of the same sentence, every round."""
    for variant in _variants():
        assert "description" not in variant["properties"]["id"]
        assert variant["properties"]["id"]["pattern"]


def test_what_every_step_shares_is_said_once_on_the_steps_array() -> None:
    """The bug, named: a phrase on every operation's `note` and `show_from`, every round --
    about thirty tokens an operation for what the steps array can say once."""
    for variant in _variants():
        assert variant["properties"]["note"] == {"type": "string"}
        assert variant["properties"]["show_from"] == {"type": "string"}
    described = _schema()["properties"]["steps"]["description"]
    assert "`note`" in described
    assert "`show_from`" in described
    assert "$id" in described


def test_the_prompt_still_explains_both_once() -> None:
    """Shortening the copies only works because the long form is said somewhere."""
    prompt = json.dumps([section.body for section in render_all(PromptContext())])
    assert "show_from" in prompt
    assert "$id" in prompt or "earlier result" in prompt


async def test_every_described_field_reaches_the_model_however_it_is_wrapped() -> None:
    """The bug, named: weftai 0.5.2 drops the description of every optional field and of every
    integer, boolean, enum or array field, so `research.open`'s `hit`, `agents.spawn`'s
    `group` and every `limit` reached the model as a bare type."""
    from datetime import UTC, datetime

    from lucy_api.packs.agents import AgentsPack
    from lucy_api.work.registry import Registry

    capabilities = Capabilities(
        (HelpPack(), AgentsPack()), work=Registry(now=lambda: datetime.now(UTC))
    )
    context = capabilities.context_for(
        SessionScope(account_id="acct", profile="personal", session_id="ses")
    )
    schema = capabilities.plan_schema(await capabilities.probe(context), "ses", context)
    spawn = next(
        variant
        for variant in schema["properties"]["steps"]["items"]["anyOf"]
        if variant["properties"]["op"].get("const") == "agents.spawn"
    )
    fields = spawn["properties"]["input"]["properties"]
    assert fields["group"]["description"].startswith("A short team name")
    assert fields["return_schema"]["description"].startswith("JSON Schema the helper must")
    assert "description" in fields["objective"], "a field already described keeps its own"


def test_no_operation_description_ends_in_a_list_of_search_keywords() -> None:
    """The bug, named: twenty-one descriptions ended in "(search, recall, remember, lookup)"
    and the like. Nothing searches descriptions, so they were tokens every round, and one
    misled: "remember" on notes.search pulled a "remember this" request towards a read."""
    import re
    from importlib.resources import files

    # A description string closes with `."`; a docstring with `."""`, and is left alone.
    tags = re.compile(r' \((?:[a-z][a-z ]*, )+[a-z][a-z ]*\)\."(?!")')
    packs = files("lucy_api.packs")
    for module in packs.iterdir():
        if module.name.endswith(".py"):
            text = " ".join(module.read_text(encoding="utf-8").replace('"\n', '" ').split())
            text = text.replace('" "', "")
            assert not tags.search(text), module.name


async def test_a_field_says_its_default_where_a_small_model_would_guess_it() -> None:
    """The bug, named: `timeout_ms` read as seconds, a device's name passed as `device_id`,
    `wait` and `limit` with nothing said about what leaving them out does."""
    from test_workspace_pack import setup

    _fake, capabilities, context = setup()
    schema = capabilities.plan_schema(await capabilities.probe(context), "sess-a", context)
    run = next(
        variant
        for variant in schema["properties"]["steps"]["items"]["anyOf"]
        if variant["properties"]["op"].get("const") == "workspace.run"
    )
    fields = run["properties"]["input"]["properties"]
    assert "milliseconds" in fields["timeout_ms"]["description"]
    assert "Default true" in fields["wait"]["description"]
    assert "Default false" in fields["wake"]["description"]

    from lucy_api.packs.music import DEVICE

    assert "music.devices" in DEVICE
