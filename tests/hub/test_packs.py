"""Installed packs become a registry the loop can execute without HTTP."""

from __future__ import annotations

import json

from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope


async def test_help_is_ready_and_lists_itself() -> None:
    capabilities = Capabilities()
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    catalogue = await capabilities.probe(context)
    listed = capabilities.listings(catalogue)

    assert listed[0]["id"] == "help"
    assert listed[0]["state"] == "ready"

    result = await capabilities.execute(
        {"steps": [{"id": "list", "op": "capabilities.list", "input": {}}]},
        context,
    )
    assert result["issues"] is None
    names = [item["id"] for item in result["steps"][0]["data"]["capabilities"]]
    assert "help" in names


async def test_use_remembers_a_pack_for_the_next_turn() -> None:
    capabilities = Capabilities()
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "use", "op": "capabilities.use", "input": {"id": "help"}}]},
        context,
    )

    assert result["steps"][0]["data"]["bound"] is True
    assert capabilities.recent("ses_a") == ("help",)


async def test_the_plan_schema_documents_show_from_on_each_step() -> None:
    capabilities = Capabilities()
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    catalogue = await capabilities.probe(context)
    schema = capabilities.plan_schema(catalogue, "ses_a", context)
    assert "show_from" in json.dumps(schema)


async def test_execute_accepts_a_step_that_only_asks_where_to_show_from() -> None:
    """``show_from`` is Lucy's, not an operation argument, and must not fail the plan."""
    capabilities = Capabilities()
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "list",
                    "op": "capabilities.list",
                    "input": {},
                    "show_from": "help",
                }
            ]
        },
        context,
    )
    assert result["issues"] is None
    assert result["steps"][0]["status"] == "ok"
