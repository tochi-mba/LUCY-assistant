"""MCP skills are hash-pinned docs; tasks are opt-in and never leak across sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from test_mcp_api import META, _headers, _rpc

from lucy_api.mcp.protocol import GONE, INVALID, TASKS_CAP, UNKNOWN_METHOD
from lucy_api.mcp.skills import (
    CATALOGUE,
    MAX_BYTES,
    MAX_FILES,
    SCHEME,
    listed,
    resolve,
)
from lucy_api.mcp.tasks import META_CLIENT_CAPS, client_offers_tasks
from lucy_api.settings.groups import service_names
from lucy_api.work import Brief, Kind, UnknownWorkError

if TYPE_CHECKING:
    from httpx import AsyncClient


def _tasks_body(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        **(params or {}),
        "_meta": {**META, META_CLIENT_CAPS: {TASKS_CAP: {}}},
    }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": payload}


async def _tasks_rpc(client: AsyncClient, method: str, params: dict[str, Any] | None = None) -> Any:
    return await client.post(
        "/mcp",
        headers=_headers(method),
        json=_tasks_body(method, params),
    )


def test_the_skill_corpus_stays_inside_the_spec_caps() -> None:
    assert 0 < len(CATALOGUE) <= MAX_FILES
    for skill in CATALOGUE:
        assert skill.size <= MAX_BYTES
        assert skill.digest.startswith("sha256:")
        assert skill.uri.startswith(SCHEME)
        lower = skill.body.lower()
        for name in service_names():
            assert name.lower() not in lower, name


def test_skill_digests_are_stable_and_change_when_the_bytes_change() -> None:
    talking = resolve("talking")
    assert talking is not None
    again = resolve(talking.uri)
    assert again is not None
    assert talking.digest == again.digest
    assert talking.document()["content"] == talking.body
    listed_names = [row["name"] for row in listed()["skills"]]
    assert listed_names == [skill.name for skill in CATALOGUE]
    assert listed()["cacheScope"] == "public"
    assert resolve("skill://lucy/") is None
    assert resolve("") is None


def test_a_finished_task_reports_its_real_state() -> None:
    from lucy_api.mcp.tasks import _task
    from lucy_api.work.types import Record, State

    record = Record(
        id="wrk_1",
        kind=Kind.helper,
        role="reviewer",
        objective="Check the notes",
        session_id="ses_1",
        started_at=datetime(2026, 9, 17, tzinfo=UTC),
        state=State.cancelled,
        detail="stopped",
    )
    payload = _task(record)
    assert payload["status"] == "cancelled"
    assert payload["progress"] == "stopped"
    assert client_offers_tasks({}) is False
    assert client_offers_tasks({"_meta": "nope"}) is False
    assert client_offers_tasks({"_meta": {}, "capabilities": {TASKS_CAP: {}}}) is True
    assert client_offers_tasks({"capabilities": {"tasks": {}}}) is True
    assert client_offers_tasks({"_meta": {META_CLIENT_CAPS: {TASKS_CAP: {}}}}) is True
    assert client_offers_tasks({"_meta": {META_CLIENT_CAPS: {}}, "capabilities": 1}) is False


@pytest.mark.asyncio
async def test_skills_list_and_get_are_hash_pinned(client: AsyncClient) -> None:
    listed_response = await _rpc(client, "skills/list")
    assert listed_response.status_code == 200, listed_response.text
    rows = listed_response.json()["result"]["skills"]
    talking = next(row for row in rows if row["name"] == "talking")
    fetched = await _rpc(client, "skills/get", {"name": "talking"})
    assert fetched.status_code == 200, fetched.text
    document = fetched.json()["result"]
    assert document["digest"] == talking["digest"]
    assert document["uri"] == talking["uri"]
    assert "lucy_session_create" in document["content"]
    by_id = await _rpc(client, "skills/get", {"id": "talking"})
    assert by_id.json()["result"]["digest"] == talking["digest"]
    by_uri = await _rpc(client, "skills/get", {"uri": talking["uri"]})
    assert by_uri.json()["result"]["digest"] == talking["digest"]


@pytest.mark.asyncio
async def test_unknown_or_missing_skills_are_invalid_params(client: AsyncClient) -> None:
    missing = await _rpc(client, "skills/get", {})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == INVALID
    unknown = await _rpc(client, "skills/get", {"name": "not-a-skill"})
    assert unknown.status_code == 400
    resource = await _rpc(client, "resources/read", {"uri": "https://example.com/x"})
    assert resource.status_code == 400
    nameless = await _rpc(client, "resources/read", {})
    assert nameless.status_code == 400


@pytest.mark.asyncio
async def test_skill_resources_are_readable_only_on_the_lucy_scheme(
    client: AsyncClient,
) -> None:
    response = await _rpc(client, "resources/read", {"uri": "skill://lucy/approvals"})
    assert response.status_code == 200, response.text
    contents = response.json()["result"]["contents"]
    assert contents[0]["uri"] == "skill://lucy/approvals"
    assert "input_required" in contents[0]["text"]
    as_name = await _rpc(client, "resources/read", {"uri": "approvals"})
    assert as_name.status_code == 400


@pytest.mark.asyncio
async def test_tasks_without_the_extension_look_like_any_unknown_method(
    client: AsyncClient,
) -> None:
    response = await _rpc(client, "tasks/list", {"session_id": "ses_x"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == UNKNOWN_METHOD


@pytest.mark.asyncio
async def test_tasks_list_get_and_cancel_are_session_bound(client: AsyncClient) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]
    other = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    other_id = other.json()["result"]["structuredContent"]["session_id"]

    empty = await _tasks_rpc(client, "tasks/list", {"session_id": session_id})
    assert empty.status_code == 200, empty.text
    assert empty.json()["result"]["tasks"] == []
    ghost = await _tasks_rpc(
        client, "tasks/get", {"session_id": session_id, "task_id": "wrk_nobody"}
    )
    assert ghost.json()["result"]["isError"] is True
    assert ghost.json()["result"]["content"][0]["text"] == GONE

    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "done"

    async def also_slow() -> str:
        await release.wait()
        return "done"

    app = client._transport.app  # type: ignore[attr-defined]
    handle = app.state.container.work.start(
        slow(),
        Brief(
            session_id=session_id,
            kind=Kind.helper,
            role="reviewer",
            objective="Read the notes without writing anything",
        ),
    )
    extra = app.state.container.work.start(
        also_slow(),
        Brief(
            session_id=session_id,
            kind=Kind.job,
            role="download",
            objective="Fetch a file in the background",
        ),
    )
    listed_tasks = await _tasks_rpc(client, "tasks/list", {"session_id": session_id})
    ids = {row["taskId"] for row in listed_tasks.json()["result"]["tasks"]}
    assert ids == {handle.id, extra.id}
    fetched = await _tasks_rpc(client, "tasks/get", {"session_id": session_id, "task_id": extra.id})
    assert fetched.json()["result"]["task"]["status"] == "working"
    stranger = await _tasks_rpc(
        client, "tasks/cancel", {"session_id": other_id, "taskId": handle.id}
    )
    assert stranger.json()["result"]["isError"] is True
    assert stranger.json()["result"]["content"][0]["text"] == GONE
    cancelled = await _tasks_rpc(
        client, "tasks/cancel", {"session_id": session_id, "taskId": handle.id}
    )
    assert cancelled.json()["result"]["task"]["taskId"] == handle.id
    for _ in range(20):
        await asyncio.sleep(0)
        alive = {record.id for record in app.state.container.work.running(session_id)}
        if handle.id not in alive:
            break
    missing = await _tasks_rpc(
        client, "tasks/cancel", {"session_id": session_id, "taskId": handle.id}
    )
    assert missing.json()["result"]["isError"] is True
    leftover = await _tasks_rpc(
        client, "tasks/cancel", {"session_id": session_id, "taskId": extra.id}
    )
    assert leftover.status_code == 200
    release.set()


@pytest.mark.asyncio
async def test_task_calls_need_a_session_and_an_id(client: AsyncClient) -> None:
    no_session = await _tasks_rpc(client, "tasks/list", {})
    assert no_session.status_code == 400
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]
    no_id = await _tasks_rpc(client, "tasks/get", {"session_id": session_id})
    assert no_id.status_code == 400
    unknown = await _tasks_rpc(client, "tasks/explode", {"session_id": session_id})
    assert unknown.status_code == 404
    gone = await _tasks_rpc(client, "tasks/list", {"session_id": "ses_missing"})
    assert gone.json()["result"]["isError"] is True
    no_id_cancel = await _tasks_rpc(client, "tasks/cancel", {"session_id": session_id})
    assert no_id_cancel.status_code == 400

    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "done"

    app = client._transport.app  # type: ignore[attr-defined]
    handle = app.state.container.work.start(
        slow(),
        Brief(
            session_id=session_id,
            kind=Kind.job,
            role="download",
            objective="Fetch a file the person asked for",
        ),
    )

    def boom(work_id: str) -> None:
        raise UnknownWorkError(work_id)

    app.state.container.work.cancel = boom  # type: ignore[method-assign]
    vanished = await _tasks_rpc(
        client, "tasks/cancel", {"session_id": session_id, "taskId": handle.id}
    )
    assert vanished.json()["result"]["isError"] is True
    release.set()
