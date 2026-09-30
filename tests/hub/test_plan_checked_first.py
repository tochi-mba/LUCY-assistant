"""A plan the executor would refuse is sent back to the model and never asked about.

The bug, named, found by reviewing the replay of approved calls on 2026-09-30: the gate saw
a plan before the executor checked it, so a plan with duplicate ids, a bad reference or a
step without an operation was asked about and approved, then refused, or replayed as
something else. A plan is now checked the way the executor checks one first; what cannot
run is the model's to repair, and nobody is asked to approve it.
"""

from __future__ import annotations

from typing import Any

from vault_pack import Vault

from lucy_api.clients.testing import FakeHttp
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope


async def _execute(plan: dict[str, Any]) -> dict[str, Any]:
    capabilities = Capabilities((HelpPack(), Vault()))
    scope = SessionScope(
        account_id="acct", profile="personal", session_id="ses", permission_mode="ask"
    )
    context = capabilities.context_for(scope, http=FakeHttp())
    await capabilities.probe(context)
    return await capabilities.execute(plan, context)


def _codes(result: dict[str, Any]) -> list[str]:
    return [str(issue.get("code")) for issue in result.get("issues") or ()]


async def test_a_plan_that_would_run_is_asked_about() -> None:
    result = await _execute(
        {
            "steps": [
                {"id": "found", "op": "vault.find", "input": {"name": "x"}},
                {"id": "play", "op": "vault.play", "input": {"record": "$found"}},
            ]
        }
    )
    assert _codes(result) == ["permission_required"]
    assert result["issues"][0]["step"] == "play"


async def test_a_reference_to_nothing_is_the_model_s_to_repair_not_the_person_s_to_approve() -> (
    None
):
    result = await _execute(
        {"steps": [{"id": "play", "op": "vault.play", "input": {"record": "$nothing"}}]}
    )
    assert _codes(result) == ["ref.unknown_target"]
    assert result["steps"] == []


async def test_a_plan_whose_ids_repeat_is_refused_before_anyone_is_asked() -> None:
    """The bug, named: only the mark was asked about and approved, and the replay ran the
    unapproved find under that id instead of the mark."""
    result = await _execute(
        {
            "steps": [
                {"id": "x", "op": "vault.find", "input": {"name": "a"}},
                {"id": "x", "op": "vault.mark", "input": {"name": "b"}},
            ]
        }
    )
    assert _codes(result) == ["step.duplicate_id"]


async def test_a_step_without_an_operation_is_refused_before_anyone_is_asked() -> None:
    """The bug, named: the gate read ``operation`` where the executor reads only ``op``, so
    the mark was asked about, approved, and replayed with no operation at all."""
    result = await _execute(
        {"steps": [{"id": "m", "operation": "vault.mark", "input": {"name": "b"}}]}
    )
    assert _codes(result) == ["plan.invalid_shape"]


async def test_a_reference_written_into_a_plain_field_is_refused_before_anyone_is_asked() -> None:
    result = await _execute(
        {
            "steps": [
                {"id": "found", "op": "vault.find", "input": {"name": "x"}},
                {"id": "tag", "op": "vault.mark", "input": {"name": "$found"}},
            ]
        }
    )
    assert _codes(result) == ["ref.in_plain_field"]
