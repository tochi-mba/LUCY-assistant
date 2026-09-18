"""Direct tool invocation uses the same gate as a turn, and burns no model tokens."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from conftest import bearer

from lucy_api.core.errors import LucyError
from lucy_api.packs.base import Availability, Bound, Catalogue, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.service import Capabilities, _unknown_tool
from lucy_api.permissions.gate import PermissionGate
from lucy_api.sessions.scope import SessionScope

if TYPE_CHECKING:
    from httpx import AsyncClient


def _notes_catalogue() -> Catalogue:
    pack = NotesPack("http://memory.test")
    return Catalogue(
        bound=(
            Bound(
                pack=pack,
                availability=Availability(state=State.ready),
                operations=(
                    SimpleNamespace(name="notes.remember", effects="write", description=""),
                    SimpleNamespace(name="notes.forget", effects="write", description=""),
                    SimpleNamespace(name="notes.search", effects="read", description=""),
                ),
            ),
        )
    )


async def test_a_read_tool_runs_without_burning_a_model_token(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/tools/help.operation/invoke",
        json={"input": {"name": "help.operation"}},
        headers=bearer(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool"] == "help.operation"
    assert body["steps"]


async def test_an_unknown_tool_names_what_this_turn_can_actually_call(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/tools/not.a.tool/invoke",
        json={"input": {}},
        headers=bearer(),
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert "not.a.tool" in detail
    assert "help.operation" in detail


async def test_a_write_in_ask_mode_is_refused_until_it_is_granted() -> None:
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    context.catalogue = _notes_catalogue()
    context.permission_mode = "ask"

    with pytest.raises(LucyError) as caught:
        await capabilities.invoke(
            "notes.remember",
            {"title": "tea", "body": "green"},
            context,
        )

    assert caught.value.status == 409
    assert "approval" in str(caught.value)


def test_auto_mode_names_the_writes_it_let_through_without_asking() -> None:
    verdict = PermissionGate().inspect(
        {"steps": [{"op": "notes.remember", "input": {"title": "tea"}}]},
        mode="auto",
        grants={},
        catalogue=_notes_catalogue(),
    )

    assert verdict.allowed is True
    assert verdict.auto_bypassed == ("notes.write",)


def test_two_writes_in_one_plan_are_both_named_so_a_subset_can_be_answered() -> None:
    verdict = PermissionGate().inspect(
        {
            "steps": [
                {"op": "notes.remember", "note": "Keep tea", "input": {"title": "tea"}},
                {"op": "notes.forget", "note": "Drop coffee", "input": {"id": "mem_1"}},
            ]
        },
        mode="ask",
        grants={},
        catalogue=_notes_catalogue(),
    )

    assert verdict.allowed is False
    assert [item.operation for item in verdict.blocked] == ["notes.remember", "notes.forget"]
    assert verdict.blocked[0].arguments == {"title": "tea"}
    assert verdict.blocked[1].arguments == {"id": "mem_1"}


async def test_invoke_and_the_tool_list_can_be_scoped_to_a_session(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "invoke-session"}
    )
    session_id = created.json()["id"]
    listed = await client.get("/v1/tools", params={"session_id": session_id}, headers=bearer())
    invoked = await client.post(
        "/v1/tools/help.operation/invoke",
        json={"input": {"name": "help.operation"}, "session_id": session_id},
        headers=bearer(),
    )

    assert created.status_code == 201, created.text
    assert listed.status_code == 200, listed.text
    assert "help.operation" in [tool["name"] for tool in listed.json()["tools"]]
    assert invoked.status_code == 200, invoked.text


async def test_invoke_probes_when_the_catalogue_has_not_been_built() -> None:
    capabilities = Capabilities((HelpPack(),))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    result = await capabilities.invoke("help.operation", {"name": "capabilities.list"}, context)
    assert result["steps"]
    with pytest.raises(LucyError) as missing:
        await capabilities.invoke("gadget.ping", {}, context)
    assert missing.value.status == 404


async def test_invoke_raises_when_the_plan_returns_an_issue() -> None:
    capabilities = Capabilities((HelpPack(),))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    await capabilities.probe(context)

    async def broken(_plan: object, _context: object) -> dict[str, object]:
        return {"issues": ["not a dict"], "steps": []}

    capabilities.execute = broken  # type: ignore[method-assign]
    with pytest.raises(LucyError) as failed:
        await capabilities.invoke("help.operation", {"name": "help.operation"}, context)
    assert "could not run" in str(failed.value) or failed.value.status == 409


def test_an_unknown_tool_mentions_what_was_deferred_so_it_can_be_bound() -> None:
    detail = _unknown_tool("music.play", {"help.operation"}, ["music"])
    empty = _unknown_tool("music.play", set(), [])

    assert "music.play" in detail
    assert "Deferred: music" in detail
    assert "capabilities.use" in detail
    assert "no bound tools" in empty


def test_accept_edits_lets_workspace_writes_through_and_records_them_as_bypassed() -> None:
    from lucy_api.packs.base import Permission

    class Files:
        id = "workspace"
        title = "Workspace"
        summary = ""

        def permissions(self) -> tuple[Permission, ...]:
            return (
                Permission(
                    id="workspace.files",
                    title="Edit files",
                    description="Change files in the attached workspace.",
                    risk="write",
                    covers=("workspace.write",),
                ),
            )

    catalogue = Catalogue(
        bound=(
            Bound(
                pack=Files(),  # type: ignore[arg-type]
                availability=Availability(state=State.ready),
                operations=(
                    SimpleNamespace(name="workspace.write", effects="write", description=""),
                ),
            ),
        )
    )
    verdict = PermissionGate().inspect(
        {"steps": [{"op": "workspace.write", "input": {"path": "a.txt"}}]},
        mode="accept_edits",
        grants={},
        catalogue=catalogue,
    )

    assert verdict.allowed is True
    assert verdict.auto_bypassed == ("workspace.files",)
