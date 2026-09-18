"""Uploaded files and session artifacts, isolated by the token's account."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from test_sessions_api import Hub, create

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.core.errors import LucyError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings

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


async def upload(
    hub: Hub, key: str = "file-1", name: str = "notes.txt", body: bytes = b"hello"
) -> dict[str, Any]:
    response = await hub.http.post(
        "/v1/files",
        headers={**bearer(), "Idempotency-Key": key},
        files={"file": (name, body, "text/plain")},
        data={"purpose": "upload"},
    )
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


class TestFiles:
    async def test_an_upload_round_trips_and_does_not_put_a_disk_path_on_the_wire(
        self, hub: Hub
    ) -> None:
        created = await upload(hub)

        assert created["filename"] == "notes.txt"
        assert created["bytes"] == 5
        assert "path" not in created
        listed = await hub.http.get("/v1/files", headers=bearer())
        assert listed.status_code == 200
        assert listed.json()["data"][0]["id"] == created["id"]
        meta = await hub.http.get(f"/v1/files/{created['id']}", headers=bearer())
        assert meta.status_code == 200
        assert meta.json()["filename"] == "notes.txt"
        content = await hub.http.get(f"/v1/files/{created['id']}/content", headers=bearer())
        assert content.status_code == 200
        assert content.content == b"hello"
        assert "notes.txt" in content.headers["content-disposition"]

    async def test_an_upload_larger_than_the_ceiling_is_413(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("lucy_api.blobs.store.MAX_BYTES", 4)
        response = await hub.http.post(
            "/v1/files",
            headers={**bearer(), "Idempotency-Key": "big"},
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 413

    async def test_a_hostile_filename_is_stored_as_a_single_path_segment(self, hub: Hub) -> None:
        created = await upload(hub, name="../../etc/passwd")

        assert created["filename"] == "passwd"

    async def test_the_same_idempotency_key_returns_the_original_file(self, hub: Hub) -> None:
        first = await upload(hub, key="same")
        again = await upload(hub, key="same")

        assert again["id"] == first["id"]

    async def test_reusing_a_key_with_a_different_body_is_a_conflict(self, hub: Hub) -> None:
        await upload(hub, key="same", body=b"one")
        response = await hub.http.post(
            "/v1/files",
            headers={**bearer(), "Idempotency-Key": "same"},
            files={"file": ("notes.txt", b"two", "text/plain")},
        )

        assert response.status_code == 409

    async def test_another_account_sees_a_miss_not_a_refusal(self, hub: Hub) -> None:
        created = await upload(hub)

        response = await hub.http.get(
            f"/v1/files/{created['id']}", headers=bearer(account_id=OTHER)
        )

        assert response.status_code == 404

    async def test_deleting_a_file_forgets_the_bytes(self, hub: Hub) -> None:
        created = await upload(hub)

        gone = await hub.http.delete(f"/v1/files/{created['id']}", headers=bearer())
        after = await hub.http.get(f"/v1/files/{created['id']}", headers=bearer())

        assert gone.status_code == 204
        assert after.status_code == 404

    async def test_bytes_that_vanish_from_disk_are_a_miss(self, hub: Hub) -> None:
        created = await upload(hub)
        row = await hub.container.blobs.get(ACCOUNT, created["id"])
        Path(str(row["path"])).unlink()  # noqa: ASYNC240 - the test is the blocking call
        response = await hub.http.get(f"/v1/files/{created['id']}/content", headers=bearer())
        assert response.status_code == 404


class TestArtifacts:
    async def test_an_artifact_is_listed_on_its_session_and_hidden_from_a_stranger(
        self, hub: Hub
    ) -> None:
        session = await create(hub)
        artifact = await hub.container.blobs.put_artifact(
            ACCOUNT,
            session["id"],
            name="out.txt",
            data=b"abc",
            mime_type="text/plain",
            produced_by="workspace.write",
        )

        listed = await hub.http.get(f"/v1/sessions/{session['id']}/artifacts", headers=bearer())
        assert listed.status_code == 200
        assert listed.json()["data"][0]["id"] == artifact["id"]
        meta = await hub.http.get(f"/v1/artifacts/{artifact['id']}", headers=bearer())
        assert meta.status_code == 200
        assert "path" not in meta.json()
        content = await hub.http.get(f"/v1/artifacts/{artifact['id']}/content", headers=bearer())
        assert content.content == b"abc"
        stranger = await hub.http.get(
            f"/v1/artifacts/{artifact['id']}", headers=bearer(account_id=OTHER)
        )
        assert stranger.status_code == 404

    async def test_an_artifact_larger_than_the_ceiling_is_refused(
        self, hub: Hub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await create(hub)
        monkeypatch.setattr("lucy_api.blobs.store.MAX_BYTES", 4)
        with pytest.raises(LucyError) as raised:
            await hub.container.blobs.put_artifact(
                ACCOUNT,
                session["id"],
                name="out.txt",
                data=b"hello",
                mime_type="text/plain",
                produced_by="workspace.write",
            )
        assert raised.value.status == 413

    async def test_a_missing_session_has_no_artifacts(self, hub: Hub) -> None:
        response = await hub.http.get("/v1/sessions/ses_nobody/artifacts", headers=bearer())
        assert response.status_code == 404

    async def test_deleting_a_session_takes_its_artifacts_with_it(self, hub: Hub) -> None:
        session = await create(hub)
        artifact = await hub.container.blobs.put_artifact(
            ACCOUNT,
            session["id"],
            name="out.txt",
            data=b"abc",
            mime_type="text/plain",
            produced_by="workspace.write",
        )

        await hub.http.delete(f"/v1/sessions/{session['id']}", headers=bearer())
        after = await hub.http.get(f"/v1/artifacts/{artifact['id']}", headers=bearer())
        assert after.status_code == 404


class TestErasure:
    async def test_erasing_the_account_drops_sessions_and_uploads_and_leaves_a_stranger_untouched(
        self, hub: Hub
    ) -> None:
        session = await create(hub)
        uploaded = await upload(hub)
        other = await hub.http.post(
            "/v1/sessions",
            json={},
            headers={**bearer(account_id=OTHER), "Idempotency-Key": "other-session"},
        )
        assert other.status_code == 201

        hooked = await hub.http.post(
            "/v1/webhooks", json={"url": "https://8.8.8.8/lucy"}, headers=bearer()
        )
        assert hooked.status_code == 201, hooked.text
        erased = await hub.http.delete("/v1/account", headers=bearer())
        assert erased.status_code == 204
        remaining = await hub.http.get("/v1/webhooks", headers=bearer())
        assert remaining.status_code == 200
        assert remaining.json()["data"] == []
        assert (
            await hub.http.get(f"/v1/sessions/{session['id']}", headers=bearer())
        ).status_code == 404
        assert (
            await hub.http.get(f"/v1/files/{uploaded['id']}", headers=bearer())
        ).status_code == 404
        still = await hub.http.get(
            f"/v1/sessions/{other.json()['id']}", headers=bearer(account_id=OTHER)
        )
        assert still.status_code == 200

    async def test_erasure_keeps_going_when_a_workspace_delete_fails(self, hub: Hub) -> None:
        await create(hub)

        class Boom(FakeEnvironmentsClient):
            async def delete(
                self, environment_id: str, path: str, *, recursive: bool = False
            ) -> None:
                del environment_id, path, recursive
                raise KeyError("gone")

        hub.container.environment_override = Boom()
        erased = await hub.http.delete("/v1/account", headers=bearer())
        listed = await hub.http.get("/v1/sessions", headers=bearer())
        assert erased.status_code == 204
        assert listed.json()["data"] == []
