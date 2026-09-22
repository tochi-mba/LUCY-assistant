"""The model catalogue over HTTP: three sections, one row, and a 404 that names the route.

What is pinned here is the contract a client renders from: every provider appears once,
a configured provider moves between sections on the strength of a real check, an unknown
provider is a problem document rather than a guess, and nothing leaks a key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
from asgi_lifespan import LifespanManager
from conftest import bearer, build_settings
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.model.catalogue import CATALOGUE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from lucy_api.clients.testing import FakeKeyring


class Answering:
    """A transport that serves the fake keyring and answers one provider's listing call."""

    def __init__(self, keyring: FakeKeyring, listing: dict[str, httpx.Response]) -> None:
        self._keyring = keyring.transport()
        self._listing = listing

    def transport(self) -> httpx.AsyncBaseTransport:
        keyring = self._keyring
        listing = self._listing

        async def handle(request: httpx.Request) -> httpx.Response:
            answer = listing.get(str(request.url))
            if answer is not None:
                return answer
            return await keyring.handle_async_request(request)

        return httpx.MockTransport(handle)


async def hub(
    keyring: FakeKeyring, listing: dict[str, httpx.Response] | None = None, **overrides: Any
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        build_settings(**overrides), transport=Answering(keyring, listing or {}).transport()
    )
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http,
    ):
        await app.state.container.preferences.aclose()
        app.state.container.preferences = FakeSettingsClient()
        yield http


async def test_the_catalogue_is_listed_in_three_sections_and_needs_a_token(
    keyring: FakeKeyring,
) -> None:
    async for client in hub(keyring):
        anonymous = await client.get("/v1/models")
        assert anonymous.status_code == 401

        response = await client.get("/v1/models", headers=bearer())
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"ready", "available", "unavailable"}
        placed = [row["provider"] for section in body.values() for row in section]
        assert sorted(placed) == sorted(spec.id for spec in CATALOGUE)
        assert body["ready"] == []
        assert body["available"] == []
        deepseek = next(row for row in body["unavailable"] if row["provider"] == "deepseek")
        assert deepseek["setup"]["command"] == "lucy models connect deepseek"


async def test_a_configured_provider_is_available_until_checked_and_ready_after(
    keyring: FakeKeyring,
) -> None:
    listing = {"https://api.deepseek.com/models": httpx.Response(200, json={"data": []})}
    async for client in hub(keyring, listing, model_keys={"deepseek": "sk-live"}):
        before = (await client.get("/v1/models", headers=bearer())).json()
        assert [row["provider"] for row in before["available"]] == ["deepseek"]
        assert "sk-live" not in str(before), "a key never appears in a report"

        after = (await client.get("/v1/models?check=true", headers=bearer())).json()
        assert [row["provider"] for row in after["ready"]] == ["deepseek"]
        assert after["ready"][0]["detail"] == "answered"
        assert "setup" not in after["ready"][0]


async def test_a_key_the_provider_refuses_is_unavailable_with_the_fix(
    keyring: FakeKeyring,
) -> None:
    listing = {"https://api.deepseek.com/models": httpx.Response(401)}
    async for client in hub(keyring, listing, model_keys={"deepseek": "sk-bad"}):
        response = await client.get("/v1/models/deepseek?check=true", headers=bearer())
        assert response.status_code == 200, response.text
        row = response.json()
        assert row["section"] == "unavailable"
        assert row["detail"] == "the key was refused"
        assert row["setup"]["command"] == "lucy models connect deepseek"


async def test_one_provider_can_be_read_on_its_own(keyring: FakeKeyring) -> None:
    async for client in hub(keyring):
        response = await client.get("/v1/models/ollama", headers=bearer())
        assert response.status_code == 200, response.text
        row = response.json()
        assert row["provider"] == "ollama"
        assert row["local"] is True
        assert row["section"] == "unavailable"
        assert "Start Ollama on this machine" in row["setup"]["instructions"]


async def test_an_unknown_provider_is_a_problem_that_names_the_listing(
    keyring: FakeKeyring,
) -> None:
    async for client in hub(keyring):
        response = await client.get("/v1/models/gemeni", headers=bearer())
        assert response.status_code == 404, response.text
        problem = response.json()
        assert "unknown-provider" in problem["type"]
        assert problem["status"] == 404
        assert "GET /v1/models" in problem["detail"]
        assert "gemeni" in problem["detail"]
