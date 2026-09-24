"""The grants ledger is addressable over HTTP, and a stranger cannot see it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from conftest import bearer

if TYPE_CHECKING:
    from httpx import AsyncClient as Client

    from lucy_api.packs.base import Permission

OTHER = "acct_someone_else"


async def test_a_grant_round_trips_and_is_invisible_to_another_account(client: Client) -> None:
    created = await client.put(
        "/v1/permissions",
        json={
            "permission": "notes.write",
            "decision": "allow",
            "profile": "personal",
            "instruction": "Keep notes.",
        },
        headers=bearer(),
    )
    listed = await client.get("/v1/permissions", headers=bearer())
    stranger = await client.get("/v1/permissions", headers=bearer(OTHER))
    forgotten = await client.delete(
        "/v1/permissions/notes.write",
        params={"profile": "personal"},
        headers=bearer(),
    )
    missing = await client.delete(
        "/v1/permissions/notes.write",
        params={"profile": "personal"},
        headers=bearer(),
    )

    assert created.status_code == 200
    assert created.json()["permission"] == "notes.write"
    assert created.json()["decision"] == "allow"
    permissions = {row["id"]: row for row in listed.json()["data"]}
    stranger_permissions = {row["id"]: row for row in stranger.json()["data"]}
    assert permissions["notes.write"]["grant"]["decision"] == "allow"
    assert permissions["notes.write"]["grant"]["instruction"] == "Keep notes."
    assert {"notes.remember", "notes.confirm"} <= set(permissions["notes.write"]["covers"])
    assert "notes.forget" not in permissions["notes.write"]["covers"]
    erase = next(row for row in listed.json()["data"] if row["id"] == "notes.erase")
    assert erase["covers"] == ["notes.forget", "notes.unlearn"]
    assert stranger_permissions["notes.write"]["grant"] is None
    assert forgotten.status_code == 204
    assert missing.status_code == 404


async def test_an_unknown_decision_names_the_values_this_collection_has(client: Client) -> None:
    response = await client.put(
        "/v1/permissions",
        json={"permission": "notes.write", "decision": "maybe"},
        headers=bearer(),
    )

    assert response.status_code == 400
    assert "allow" in response.json()["detail"]
    assert "deny" in response.json()["detail"]


async def test_a_profile_grant_overrides_an_account_grant(client: Client) -> None:
    await client.put(
        "/v1/permissions",
        json={"permission": "notes.write", "decision": "deny", "profile": "*"},
        headers=bearer(),
    )
    await client.put(
        "/v1/permissions",
        json={"permission": "notes.write", "decision": "allow", "profile": "work"},
        headers=bearer(),
    )

    personal = await client.get("/v1/permissions", params={"profile": "personal"}, headers=bearer())
    work = await client.get("/v1/permissions", params={"profile": "work"}, headers=bearer())

    personal_note = next(row for row in personal.json()["data"] if row["id"] == "notes.write")
    work_note = next(row for row in work.json()["data"] if row["id"] == "notes.write")
    assert personal_note["grant"]["decision"] == "deny"
    assert personal_note["grant"]["profile"] == "*"
    assert work_note["grant"]["decision"] == "allow"
    assert work_note["grant"]["profile"] == "work"


async def test_duplicate_permission_ids_are_listed_once(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = client._transport.app  # type: ignore[attr-defined]
    pack = next(item for item in app.state.container.capabilities.packs if item.permissions())
    original = pack.permissions
    first = original()[0]

    def duplicated() -> tuple[Permission, ...]:
        return (*original(), first)

    monkeypatch.setattr(pack, "permissions", duplicated)
    listed = await client.get("/v1/permissions", headers=bearer())
    ids = [row["id"] for row in listed.json()["data"]]
    assert ids.count(first.id) == 1
