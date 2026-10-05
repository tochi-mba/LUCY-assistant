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
