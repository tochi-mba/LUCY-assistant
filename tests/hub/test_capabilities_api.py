"""The catalogue and the bound registry, over HTTP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from conftest import bearer

if TYPE_CHECKING:
    from httpx import AsyncClient


async def _session(client: AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "cap-session"}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_capabilities_are_listed_with_state_and_without_an_account_id(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/capabilities", headers=bearer())

    assert response.status_code == 200, response.text
    body = response.json()
    help_pack = next(item for item in body["data"] if item["id"] == "help")
    assert help_pack["state"] == "ready"
    assert help_pack["usable"] is True
    assert "account_id" not in str(body)


async def test_the_model_tools_include_the_help_operations(client: AsyncClient) -> None:
    response = await client.get("/v1/tools", headers=bearer())

    assert response.status_code == 200, response.text
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "capabilities.list" in names
    assert "help.operation" in names
    assert response.json()["deferred"] == []


async def test_a_session_prompt_preview_and_context_are_priced_by_band(
    client: AsyncClient,
) -> None:
    created = await _session(client)
    preview = await client.get("/v1/prompt/preview", headers=bearer())
    scoped = await client.get(
        "/v1/prompt/preview",
        params={"session_id": created["id"]},
        headers=bearer(),
    )
    context = await client.get(f"/v1/sessions/{created['id']}/context", headers=bearer())

    assert preview.status_code == 200, preview.text
    assert scoped.status_code == 200, scoped.text
    assert context.status_code == 200, context.text
    assert "system" in preview.json()["bands"]
    assert context.json()["total"] >= preview.json()["total"]
    assert "prompt" in context.json()


async def test_notes_are_listed_as_unavailable_when_memory_cannot_be_reached(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/capabilities", headers=bearer())
    notes = next(item for item in response.json()["data"] if item["id"] == "notes")
    assert notes["state"] == "unavailable"
    assert notes["usable"] is False
    created = await _session(client)
    response = await client.get(
        f"/v1/sessions/{created['id']}/context",
        headers=bearer(account_id="acct_someone_else"),
    )

    assert response.status_code == 404
