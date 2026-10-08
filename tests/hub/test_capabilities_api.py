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
    assert "help.skill" in names
    assert response.json()["deferred"] == []


async def test_no_operation_promises_a_schema_the_model_already_holds(client: AsyncClient) -> None:
    """The bug, named: `help.operation` promised "one operation's full schema and examples",
    returned neither -- it found only operations already in the plan schema, and nothing
    defines examples -- and told a small model to spend a round on it before every unfamiliar
    call. Nothing the model reads names it any more."""
    from lucy_api.mcp.skills import CATALOGUE
    from lucy_api.prompt.docs import capability_doc

    response = await client.get("/v1/tools", headers=bearer())
    names = [tool["name"] for tool in response.json()["tools"]]
    assert "help.operation" not in names
    assert "help.operation" not in capability_doc("help").read_text(encoding="utf-8")
    assert not [skill.name for skill in CATALOGUE if "help.operation" in skill.body]


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


async def test_the_listing_and_the_tools_probe_with_the_persons_own_settings(
    keyring: Any,
) -> None:
    """The bug, named: both routes probed with a default policy, so Claude Code delegation
    read "disabled" in the listing for a person who had switched it on, while every
    conversation of theirs had it ready."""
    from asgi_lifespan import LifespanManager
    from conftest import build_settings
    from httpx import ASGITransport
    from httpx import AsyncClient as Client
    from settings_client.testing import FakeSettingsClient

    from lucy_api.api.app import create_app
    from lucy_api.clients.environments import FakeEnvironmentsClient

    app = create_app(
        build_settings(coder_api_base_url="http://coder.test"), transport=keyring.transport()
    )
    async with (
        LifespanManager(app),
        Client(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        await app.state.container.preferences.aclose()
        preferences = FakeSettingsClient()
        preferences.seed(
            "lucy",
            {"claude_code_delegation": True, "claude_code_directories": ["C:/code"]},
            profile="personal",
        )
        app.state.container.preferences = preferences
        app.state.container.environment_override = FakeEnvironmentsClient()

        listed = await http.get("/v1/capabilities", headers=bearer())
        coder = next(item for item in listed.json()["data"] if item["id"] == "coder")
        # Switched on, so the probe went past the person's switches to the bridge itself,
        # which the test has no answer from; before, the switches read as off.
        assert coder["state"] == "unavailable", coder
        assert "could not be reached" in coder["detail"]

        tools = await http.get("/v1/tools", headers=bearer())
        assert tools.status_code == 200
        assert ("lucy", "personal") in preferences.asked
