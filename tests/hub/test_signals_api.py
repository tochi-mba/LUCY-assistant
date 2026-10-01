"""`POST /v1/signals/{id}`: a sibling ends a subscription, and an idle session wakes.

Signals carry no bearer token; the signature is the proof. So what is pinned here is the
door: a good signal wakes, and every bad one -- forged, unknown, late, oversized, malformed --
is refused in a way that tells an outsider nothing it did not already know.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from test_sessions_api import Hub

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.net.signing import HEADER, sign
from lucy_api.sessions.models import CreateSession
from lucy_api.work import State
from lucy_api.work.subscriptions import MAX_SIGNAL_BYTES

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings


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


async def a_subscription(hub: Hub) -> tuple[str, Any]:
    session = await hub.store.create(ACCOUNT, CreateSession(model="scripted:demo"), "signals")
    opened = await hub.container.subscriptions.open(
        account_id=ACCOUNT,
        session_id=str(session["id"]),
        profile="personal",
        capability="repos",
        objective="Say when CI on #42 is green",
        timeout_seconds=600,
    )
    return str(session["id"]), opened


async def eventually(check: Any) -> None:
    for _ in range(200):
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("it never happened")


async def test_a_signed_signal_ends_the_work_and_wakes_the_idle_session(hub: Hub) -> None:
    session_id, opened = await a_subscription(hub)
    body = json.dumps({"state": "fired", "summary": "CI on #42 is green"}).encode()

    sent = await hub.http.post(
        f"/v1/signals/{opened.subscription_id}",
        content=body,
        headers={HEADER: sign(opened.secret, body), "content-type": "application/json"},
    )

    assert sent.status_code == 204, sent.text
    assert hub.container.work.state_of(opened.handle.id) is State.succeeded

    async def woke() -> bool:
        items = await hub.store.records(ACCOUNT, session_id, "items")
        return any(item["type"] == "notice" for item in items)

    await eventually(woke)
    items = await hub.store.records(ACCOUNT, session_id, "items")
    [notice] = [item for item in items if item["type"] == "notice"]
    assert "Say when CI on #42 is green" in str(notice)
    assert "Nothing here is from the person" in str(notice)


async def test_a_forged_and_an_unknown_signal_are_the_same_404(hub: Hub) -> None:
    _, opened = await a_subscription(hub)
    body = json.dumps({"state": "fired", "summary": "green"}).encode()

    forged = await hub.http.post(
        f"/v1/signals/{opened.subscription_id}",
        content=body,
        headers={HEADER: sign("guessed", body)},
    )
    unsigned = await hub.http.post(f"/v1/signals/{opened.subscription_id}", content=body)
    unknown = await hub.http.post(
        "/v1/signals/sub_nobody", content=body, headers={HEADER: sign(opened.secret, body)}
    )

    assert forged.status_code == unsigned.status_code == unknown.status_code == 404
    assert forged.json() == unsigned.json() | {"request_id": forged.json()["request_id"]}
    assert forged.json()["detail"] == unknown.json()["detail"]
    assert forged.headers["content-type"].startswith("application/problem+json")
    assert hub.container.work.state_of(opened.handle.id) is State.running


async def test_a_late_signal_is_409_and_an_oversized_one_413(hub: Hub) -> None:
    _, opened = await a_subscription(hub)
    body = json.dumps({"state": "fired", "summary": "green"}).encode()
    url = f"/v1/signals/{opened.subscription_id}"
    first = await hub.http.post(url, content=body, headers={HEADER: sign(opened.secret, body)})
    again = await hub.http.post(url, content=body, headers={HEADER: sign(opened.secret, body)})
    huge = b"x" * (MAX_SIGNAL_BYTES + 1)
    oversized = await hub.http.post(url, content=huge, headers={HEADER: sign(opened.secret, huge)})

    assert first.status_code == 204
    assert again.status_code == 409
    assert oversized.status_code == 413


async def test_a_verified_body_that_is_not_a_signal_is_422_naming_the_shape(hub: Hub) -> None:
    _, opened = await a_subscription(hub)
    body = b'{"state": "done"}'

    refused = await hub.http.post(
        f"/v1/signals/{opened.subscription_id}",
        content=body,
        headers={HEADER: sign(opened.secret, body)},
    )

    assert refused.status_code == 422
    assert "`state` (fired, failed or expired)" in refused.json()["detail"]


async def test_the_route_is_in_the_published_contract(hub: Hub) -> None:
    schema = (await hub.http.get("/openapi.json")).json()
    operation = schema["paths"]["/v1/signals/{subscription_id}"]["post"]
    assert operation["operationId"] == "send_signal"
    assert "X-Lucy-Signature" in operation["description"]
