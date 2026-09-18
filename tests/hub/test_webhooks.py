"""Webhooks are a signal, never a transcript, and they never point at the LAN."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from test_sessions_api import Hub

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.webhooks.service import MAX_HOOKS, httpx_deliver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings

HOOK = "https://8.8.8.8/lucy/hook"
OTHER = "acct_someone_else"


@pytest.fixture
async def hub(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[Hub]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.environment_override = FakeEnvironmentsClient()
        yield Hub(http=http, store=container.store, container=container)


async def test_create_lists_without_the_secret_and_replays_the_same_url(hub: Hub) -> None:
    created = await hub.http.post("/v1/webhooks", json={"url": HOOK}, headers=bearer())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["url"] == HOOK
    assert body["secret"]
    listed = await hub.http.get("/v1/webhooks", headers=bearer())
    assert listed.status_code == 200
    row = listed.json()["data"][0]
    assert row["id"] == body["id"]
    assert "secret" not in row or row["secret"] is None
    again = await hub.http.post("/v1/webhooks", json={"url": HOOK}, headers=bearer())
    assert again.status_code == 201
    replay = again.json()
    assert replay["id"] == body["id"]
    assert replay.get("secret") is None


async def test_a_loopback_destination_is_refused_without_echoing_the_address(
    hub: Hub,
) -> None:
    refused = await hub.http.post(
        "/v1/webhooks", json={"url": "https://127.0.0.1/hook"}, headers=bearer()
    )
    assert refused.status_code == 403
    assert refused.json()["type"].endswith("/ssrf-blocked")
    assert "127.0.0.1" not in refused.text


async def test_another_account_cannot_see_or_delete_this_hook(hub: Hub) -> None:
    created = await hub.http.post("/v1/webhooks", json={"url": HOOK}, headers=bearer())
    hook_id = created.json()["id"]
    theirs = await hub.http.post(
        "/v1/webhooks", json={"url": HOOK}, headers=bearer(account_id=OTHER)
    )
    assert theirs.status_code == 201
    assert theirs.json()["id"] != hook_id
    listed = await hub.http.get("/v1/webhooks", headers=bearer())
    assert [row["id"] for row in listed.json()["data"]] == [hook_id]
    stolen = await hub.http.delete(f"/v1/webhooks/{hook_id}", headers=bearer(account_id=OTHER))
    assert stolen.status_code == 404
    missing = await hub.http.delete("/v1/webhooks/whk_missing", headers=bearer())
    assert missing.status_code == 404
    gone = await hub.http.delete(f"/v1/webhooks/{hook_id}", headers=bearer())
    assert gone.status_code == 204
    empty = await hub.http.get("/v1/webhooks", headers=bearer())
    assert empty.json()["data"] == []


async def test_the_twenty_first_destination_is_a_conflict(hub: Hub) -> None:
    for index in range(MAX_HOOKS):
        added = await hub.http.post(
            "/v1/webhooks",
            json={"url": f"https://8.8.8.8/h/{index}"},
            headers=bearer(),
        )
        assert added.status_code == 201, added.text
    overflow = await hub.http.post(
        "/v1/webhooks", json={"url": "https://8.8.8.8/h/overflow"}, headers=bearer()
    )
    assert overflow.status_code == 409
    assert overflow.json()["type"].endswith("/webhook-limit")


async def test_a_completed_turn_posts_a_signed_signal_and_skips_running(
    hub: Hub,
) -> None:
    created = await hub.http.post("/v1/webhooks", json={"url": HOOK}, headers=bearer())
    secret = created.json()["secret"]
    delivered: list[tuple[str, dict[str, str], bytes]] = []

    async def capture(url: str, headers: dict[str, str], body: bytes) -> None:
        delivered.append((url, headers, body))

    hub.container.webhooks.deliver = capture
    await hub.container.webhooks.notify(ACCOUNT, "ses_1", "trn_1", "running")
    assert delivered == []
    await hub.container.webhooks.notify(ACCOUNT, "ses_1", "trn_1", "completed")
    url, headers, body = delivered[0]
    assert url == HOOK
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert headers["x-lucy-signature"] == f"sha256={expected}"
    assert b'"status":"completed"' in body
    assert b"transcript" not in body


async def test_a_delivery_failure_does_not_fail_the_turn(hub: Hub) -> None:
    await hub.http.post("/v1/webhooks", json={"url": HOOK}, headers=bearer())

    async def boom(url: str, headers: dict[str, str], body: bytes) -> None:
        del url, headers, body
        raise RuntimeError("unreachable")

    hub.container.webhooks.deliver = boom
    await hub.container.webhooks.notify(ACCOUNT, "ses_1", "trn_1", "failed")


async def test_notify_is_a_no_op_when_nothing_will_deliver(hub: Hub) -> None:
    hub.container.webhooks.deliver = None
    await hub.http.post("/v1/webhooks", json={"url": HOOK}, headers=bearer())
    await hub.container.webhooks.notify(ACCOUNT, "ses_1", "trn_1", "completed")


async def test_httpx_deliver_posts_and_swallows_transport_errors() -> None:
    seen: list[bytes] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        await httpx_deliver(client)(
            "https://8.8.8.8/hook",
            {"content-type": "application/json"},
            b'{"status":"cancelled"}',
        )
    assert seen == [b'{"status":"cancelled"}']

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        await httpx_deliver(client)("https://8.8.8.8/hook", {}, b"{}")


async def test_an_invented_field_on_create_is_422(hub: Hub) -> None:
    response = await hub.http.post(
        "/v1/webhooks", json={"url": HOOK, "secret": "mine"}, headers=bearer()
    )
    assert response.status_code == 422
