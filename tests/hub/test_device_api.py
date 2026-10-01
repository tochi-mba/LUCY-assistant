"""The HTTP device flow signs a CLI in without accepting a password."""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import bearer

if TYPE_CHECKING:
    from httpx import AsyncClient


async def test_authenticated_client_can_approve_a_device_and_transfer_its_subject_token(
    client: AsyncClient,
) -> None:
    created = await client.post("/v1/auth/device")

    assert created.status_code == 201, created.text
    authorization = created.json()
    assert authorization["verification_uri"] == "http://test/device"
    assert authorization["user_code"] in authorization["verification_uri_complete"]
    assert authorization["interval"] == 5

    approved = await client.post(
        "/v1/auth/device/authorize",
        json={"user_code": authorization["user_code"], "approve": True},
        headers=bearer(account_id="acct_device"),
    )
    assert approved.status_code == 204, approved.text

    redeemed = await client.post(
        "/v1/auth/device/token", json={"device_code": authorization["device_code"]}
    )
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.json()["token_type"] == "Bearer"
    me = await client.get(
        "/v1/me", headers={"Authorization": f"Bearer {redeemed.json()['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["account_id"] == "acct_device"


async def test_pending_and_denied_device_codes_use_rfc_error_words(client: AsyncClient) -> None:
    pending_code = (await client.post("/v1/auth/device")).json()
    pending = await client.post(
        "/v1/auth/device/token", json={"device_code": pending_code["device_code"]}
    )
    assert pending.status_code == 400
    assert pending.json()["error"] == "authorization_pending"
    assert pending.headers["cache-control"] == "no-store"

    denied_code = (await client.post("/v1/auth/device")).json()
    denied = await client.post(
        "/v1/auth/device/authorize",
        json={"user_code": denied_code["user_code"], "approve": False},
        headers=bearer(),
    )
    assert denied.status_code == 204
    terminal = await client.post(
        "/v1/auth/device/token", json={"device_code": denied_code["device_code"]}
    )
    assert terminal.status_code == 400
    assert terminal.json()["error"] == "access_denied"


async def test_device_approval_requires_an_existing_authenticated_client(
    client: AsyncClient,
) -> None:
    created = (await client.post("/v1/auth/device")).json()
    response = await client.post(
        "/v1/auth/device/authorize",
        json={"user_code": created["user_code"], "approve": True},
    )

    assert response.status_code == 401


async def test_the_page_a_waiting_client_is_sent_to_exists_and_says_how_to_approve(
    client: AsyncClient,
) -> None:
    """The bug, named: the hub sent `lucy setup` to `/device` and served nothing there, so the
    first thing a person signing in saw was a 404."""
    created = (await client.post("/v1/auth/device")).json()
    page = await client.get(created["verification_uri_complete"].removeprefix("http://test"))

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert f"lucy approve {created['user_code']}" in page.text
    assert "<form" not in page.text
    assert 'type="password"' not in page.text


async def test_the_page_never_shows_back_a_code_the_hub_could_not_have_issued(
    client: AsyncClient,
) -> None:
    page = await client.get("/device", params={"user_code": "<script>alert(1)</script>"})
    assert page.status_code == 200
    assert "<script>" not in page.text
    assert "lucy approve CODE" in page.text
