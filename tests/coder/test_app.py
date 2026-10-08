"""The HTTP face: a token or nothing, sentences for refusals, and the six routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from conftest import ACCOUNT, a_service, fake_command
from httpx import ASGITransport, AsyncClient

from lucy_coder.app import create_app
from lucy_coder.auth import AuthenticationError, KeyringUnreachableError, VerifiedCaller
from lucy_coder.config import Settings

if TYPE_CHECKING:
    from lucy_coder.tasks import TaskStore


class FakeVerifier:
    """Tokens are account ids here; 'down' plays a keyring outage."""

    async def verify(self, token: str) -> VerifiedCaller:
        if token == "down":
            raise KeyringUnreachableError
        if not token.startswith("acct"):
            raise AuthenticationError
        return VerifiedCaller(account_id=token, audience="coder-api")


def client_for(store: TaskStore, **settings: object) -> tuple[AsyncClient, object]:
    service = a_service(store)
    fields: dict[str, object] = {"claude_command": fake_command(), **settings}
    app = create_app(
        Settings(**fields),  # type: ignore[arg-type]
        service,
        FakeVerifier(),  # type: ignore[arg-type]
    )
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://coder.test"), service


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_every_task_route_needs_a_token_and_health_does_not(store: TaskStore) -> None:
    client, service = client_for(store)
    try:
        assert (await client.get("/healthy")).status_code == 200
        for method, path in (
            ("POST", "/v1/tasks"),
            ("GET", "/v1/tasks"),
            ("GET", "/v1/tasks/tsk_1"),
            ("POST", "/v1/tasks/tsk_1/message"),
            ("POST", "/v1/tasks/tsk_1/cancel"),
        ):
            bare = await client.request(method, path, json={"text": "x"})
            assert bare.status_code == 401, path
            assert bare.json()["detail"] == "a coder-api token is required"
            wrong = await client.request(
                method, path, json={"text": "x"}, headers=bearer("not-a-token")
            )
            assert wrong.status_code == 401, path
    finally:
        await client.aclose()
        await service.aclose()


async def test_keyring_down_is_503_never_a_pass(store: TaskStore) -> None:
    client, service = client_for(store)
    try:
        answer = await client.get("/v1/tasks", headers=bearer("down"))
        assert answer.status_code == 503
        assert answer.json()["detail"] == "keyring is unreachable"
    finally:
        await client.aclose()
        await service.aclose()


async def test_a_delegation_runs_end_to_end_over_http(store: TaskStore, workdir: str) -> None:
    client, service = client_for(store)
    try:
        made = await client.post(
            "/v1/tasks",
            json={"brief": "write hello.txt saying hi", "directory": workdir, "title": "hello"},
            headers=bearer(ACCOUNT),
        )
        assert made.status_code == 201
        task = made.json()
        assert task["state"] in {"queued", "running"}
        assert task["run_level"] == "edits", "the default level is the person's default"

        import asyncio

        for _ in range(400):
            read = (await client.get(f"/v1/tasks/{task['id']}", headers=bearer(ACCOUNT))).json()
            if read["state"] == "idle":
                break
            await asyncio.sleep(0.05)
        assert read["result"] == "done: wrote hello.txt"

        listed = (await client.get("/v1/tasks", headers=bearer(ACCOUNT))).json()
        assert [row["id"] for row in listed["tasks"]] == [task["id"]]
        other = await client.get(f"/v1/tasks/{task['id']}", headers=bearer("acct_other"))
        assert other.status_code == 404

        tailed = (
            await client.get(f"/v1/tasks/{task['id']}?tail_chars=5000", headers=bearer(ACCOUNT))
        ).json()
        assert '"type": "result"' in tailed["transcript_tail"]

        said = await client.post(
            f"/v1/tasks/{task['id']}/message",
            json={"text": "now add a test"},
            headers=bearer(ACCOUNT),
        )
        assert said.status_code == 200
        assert "advice" in said.json()

        stopped = await client.post(
            f"/v1/tasks/{task['id']}/cancel", json={}, headers=bearer(ACCOUNT)
        )
        assert stopped.status_code == 200
    finally:
        await client.aclose()
        await service.aclose()


async def test_a_refusal_crosses_http_with_its_sentence_and_status(
    store: TaskStore, workdir: str
) -> None:
    client, service = client_for(store)
    try:
        bad = await client.post(
            "/v1/tasks",
            json={"brief": "x", "directory": workdir + "-gone"},
            headers=bearer(ACCOUNT),
        )
        assert bad.status_code == 422
        assert "directory does not exist" in bad.json()["detail"]
    finally:
        await client.aclose()
        await service.aclose()


async def test_the_tail_ask_is_capped_by_the_settings(store: TaskStore, workdir: str) -> None:
    client, service = client_for(store, tail_chars_max=10)
    try:
        made = await client.post(
            "/v1/tasks",
            json={"brief": "write hello.txt saying hi", "directory": workdir},
            headers=bearer(ACCOUNT),
        )
        task_id = made.json()["id"]
        import asyncio

        for _ in range(400):
            read = (
                await client.get(f"/v1/tasks/{task_id}?tail_chars=999999", headers=bearer(ACCOUNT))
            ).json()
            if read["state"] == "idle":
                break
            await asyncio.sleep(0.05)
        assert len(read["transcript_tail"]) <= 10
    finally:
        await client.aclose()
        await service.aclose()


async def test_ready_says_whether_a_delegation_would_work(store: TaskStore) -> None:
    client, service = client_for(store)
    try:
        good = await client.get("/ready")
        assert good.status_code == 200
        assert good.json() == {"status": "ok", "detail": ""}
    finally:
        await client.aclose()
        await service.aclose()

    broken_client, broken_service = client_for(store, claude_command=["claude-nowhere"])
    try:
        bad = await broken_client.get("/ready")
        assert bad.status_code == 503
        assert "not installed" in bad.json()["detail"]
    finally:
        await broken_client.aclose()
        await broken_service.aclose()


async def test_bodies_are_bounded(store: TaskStore, workdir: str) -> None:
    client, service = client_for(store)
    try:
        huge = await client.post(
            "/v1/tasks",
            json={"brief": "x" * 20_001, "directory": workdir},
            headers=bearer(ACCOUNT),
        )
        assert huge.status_code == 422
    finally:
        await client.aclose()
        await service.aclose()


@pytest.mark.parametrize("header", ["", "Token abc", "bearer   "])
async def test_a_malformed_authorization_header_is_401(store: TaskStore, header: str) -> None:
    client, service = client_for(store)
    try:
        answer = await client.get("/v1/tasks", headers={"Authorization": header} if header else {})
        assert answer.status_code == 401
    finally:
        await client.aclose()
        await service.aclose()
