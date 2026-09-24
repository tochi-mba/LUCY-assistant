"""A direct tool call scoped to a session reaches that session's workspace.

`GET /v1/tools?session_id=` and `POST /v1/tools/{name}/invoke` with a `session_id` built their
pack context without the session's workspace, which only `prepare_turn` attached. So the
workspace probed as "no workspace is attached": its operations were neither listed nor
invokable, and a session's own files could not be read or written from here at all. Found
by the eval harness's contract test, which plants a file before a conversation and reads
it back after one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import bearer

if TYPE_CHECKING:
    from httpx import AsyncClient


async def session(client: AsyncClient, key: str) -> str:
    created = await client.post(
        "/v1/sessions",
        json={"permission_mode": "accept_edits"},
        headers={**bearer(), "Idempotency-Key": key},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_a_session_s_tool_listing_includes_its_workspace(
    workspace_client: AsyncClient,
) -> None:
    session_id = await session(workspace_client, "listing")

    listed = await workspace_client.get(
        "/v1/tools", params={"session_id": session_id}, headers=bearer()
    )

    assert listed.status_code == 200, listed.text
    body = listed.json()
    names = {tool["name"] for tool in body["tools"]}
    assert "workspace.read" in names or "workspace" in body["deferred"]


async def test_an_invoke_in_a_session_writes_and_reads_that_session_s_files(
    workspace_client: AsyncClient,
) -> None:
    session_id = await session(workspace_client, "files")
    other = await session(workspace_client, "other")

    async def call(name: str, arguments: dict[str, str], scope: str) -> dict[str, object]:
        answer = await workspace_client.post(
            f"/v1/tools/{name}/invoke",
            json={"input": arguments, "session_id": scope},
            headers=bearer(),
        )
        assert answer.status_code == 200, answer.text
        step: dict[str, object] = answer.json()["steps"][0]
        return step

    await call("capabilities.use", {"id": "workspace"}, session_id)
    written = await call("workspace.write", {"path": "notes.md", "content": "Hi"}, session_id)
    read = await call("workspace.read", {"path": "notes.md"}, session_id)
    await call("capabilities.use", {"id": "workspace"}, other)
    elsewhere = await call("workspace.read", {"path": "notes.md"}, other)

    assert written["status"] == "ok"
    assert read["status"] == "ok"
    assert "Hi" in str(read["data"])
    assert "Hi" not in str(elsewhere["data"]), "another session's workspace is another subtree"
