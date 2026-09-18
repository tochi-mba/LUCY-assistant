"""Lucy speaks MCP over POST /mcp, as a dual-era Streamable HTTP server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from conftest import bearer

from lucy_api.mcp.catalog import WEFTAI_DESCRIBE_OPERATIONS
from lucy_api.mcp.protocol import (
    CURRENT,
    HEADER_MISMATCH,
    HEADER_MISMATCH_DETAIL,
    INSTRUCTIONS,
    INVALID_PARAMS,
    LEGACY,
    META_VERSION,
    ORIGIN_REFUSED,
    PARSE,
    UNKNOWN_METHOD,
    UNSUPPORTED_VERSION,
)

if TYPE_CHECKING:
    from httpx import AsyncClient

META = {
    META_VERSION: CURRENT,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _headers(
    method: str, *, name: str | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    headers = {
        **bearer(),
        "MCP-Protocol-Version": CURRENT,
        "Mcp-Method": method,
        "Mcp-Session-Id": "must-not-be-echoed",
    }
    if name is not None:
        headers["Mcp-Name"] = name
    if extra:
        headers.update(extra)
    return headers


def _body(
    method: str, params: dict[str, Any] | None = None, *, rpc_id: object = 1
) -> dict[str, Any]:
    payload: dict[str, Any] = {**(params or {}), "_meta": META}
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": payload}


async def _rpc(
    client: AsyncClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    name: str | None = None,
    extra: dict[str, str] | None = None,
) -> Any:
    return await client.post(
        "/mcp",
        headers=_headers(method, name=name, extra=extra),
        json=_body(method, params),
    )


@pytest.mark.asyncio
async def test_get_and_delete_are_not_allowed(client: AsyncClient) -> None:
    get = await client.get("/mcp", headers=bearer())
    delete = await client.delete("/mcp", headers=bearer())
    assert get.status_code == 405
    assert delete.status_code == 405
    assert get.json()["status"] == 405


@pytest.mark.asyncio
async def test_a_foreign_origin_is_403_before_the_body_is_interpreted(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/mcp",
        headers={**bearer(), "Origin": "https://evil.example"},
        json=_body("server/discover"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == ORIGIN_REFUSED
    assert "evil" not in response.text


@pytest.mark.asyncio
async def test_missing_credentials_are_401_with_resource_metadata(client: AsyncClient) -> None:
    response = await client.post("/mcp", json=_body("server/discover"))
    assert response.status_code == 401
    challenge = response.headers["WWW-Authenticate"]
    assert "resource_metadata=" in challenge
    assert "oauth-protected-resource" in challenge
    assert "scope=" in challenge


@pytest.mark.asyncio
async def test_discover_advertises_both_eras_and_is_publicly_cacheable(
    client: AsyncClient,
) -> None:
    response = await _rpc(client, "server/discover")
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in {key.lower() for key in response.headers}
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == [CURRENT, LEGACY]
    assert result["cacheScope"] == "public"
    assert result["instructions"] == INSTRUCTIONS
    assert result["capabilities"]["skills"] == {"listChanged": False}
    assert result["capabilities"]["extensions"]["io.modelcontextprotocol/tasks"] == {}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "lucy"


@pytest.mark.asyncio
async def test_legacy_initialize_does_not_need_modern_headers(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=bearer(),
        json={
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": {"name": "x"}},
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["protocolVersion"] == LEGACY
    assert result["serverInfo"]["name"] == "lucy"
    assert result["capabilities"]["skills"]["listChanged"] is False
    assert "resultType" not in result


@pytest.mark.asyncio
async def test_initialized_notification_is_accepted_with_no_body(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=bearer(),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert response.status_code == 202
    assert response.content == b""


@pytest.mark.asyncio
async def test_missing_meta_on_a_modern_request_is_invalid_params(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("server/discover"),
        json={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == INVALID_PARAMS


@pytest.mark.asyncio
async def test_an_unsupported_version_lists_supported_revisions(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers={**bearer(), "MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "server/discover"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "server/discover",
            "params": {"_meta": {META_VERSION: "1900-01-01"}},
        },
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == UNSUPPORTED_VERSION
    assert error["data"]["supported"] == [CURRENT, LEGACY]
    assert error["data"]["requested"] == "1900-01-01"


@pytest.mark.asyncio
async def test_a_method_header_mismatch_does_not_echo_the_values(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("tools/list"),
        json=_body("server/discover"),
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == HEADER_MISMATCH
    assert error["message"] == HEADER_MISMATCH_DETAIL
    assert "tools/list" not in error["message"]
    assert "server/discover" not in error["message"]


@pytest.mark.asyncio
async def test_unknown_methods_are_404_jsonrpc(client: AsyncClient) -> None:
    response = await _rpc(client, "prompts/list")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == UNKNOWN_METHOD


@pytest.mark.asyncio
async def test_malformed_json_is_a_parse_error(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("server/discover"),
        content=b"{",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == PARSE


@pytest.mark.asyncio
async def test_tools_list_is_private_and_includes_weftai_names(client: AsyncClient) -> None:
    response = await _rpc(client, "tools/list")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["cacheScope"] == "private"
    names = [tool["name"] for tool in result["tools"]]
    assert names == sorted(names)
    for name in (
        "lucy_session_create",
        "lucy_chat",
        "lucy_list_capabilities",
        "lucy_connect",
        "lucy_get_session_items",
        "lucy_run_plan",
        "lucy_get_result",
        "run_plan",
        "describe_operations",
        "get_result",
    ):
        assert name in names
    describe = next(tool for tool in result["tools"] if tool["name"] == "describe_operations")
    assert describe["description"] == WEFTAI_DESCRIBE_OPERATIONS
    run_plan = next(tool for tool in result["tools"] if tool["name"] == "run_plan")
    assert run_plan["description"].startswith("Run one or more steps")
    reads = next(tool for tool in result["tools"] if tool["name"] == "lucy_list_capabilities")
    assert reads["annotations"]["readOnlyHint"] is True
    assert reads["annotations"]["destructiveHint"] is False


@pytest.mark.asyncio
async def test_session_create_chat_and_items_are_keyed_to_the_caller(
    client: AsyncClient,
) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {"title": "MCP"}},
        name="lucy_session_create",
    )
    assert created.status_code == 200, created.text
    result = created.json()["result"]
    assert result["isError"] is False
    session_id = result["structuredContent"]["session_id"]
    assert session_id

    other = await client.post(
        "/mcp",
        headers=_headers("tools/call", name="lucy_get_session_items", extra=bearer("acct_other")),
        json=_body(
            "tools/call",
            {"name": "lucy_get_session_items", "arguments": {"session_id": session_id}},
        ),
    )
    assert other.status_code == 200
    assert other.json()["result"]["isError"] is True
    assert "expired" in other.json()["result"]["content"][0]["text"].lower()

    chat = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_chat", "arguments": {"session_id": session_id, "text": "hello"}},
        name="lucy_chat",
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["result"]["structuredContent"]["status"] == "queued"

    items = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_get_session_items", "arguments": {"session_id": session_id}},
        name="lucy_get_session_items",
    )
    assert items.status_code == 200, items.text
    page = items.json()["result"]["structuredContent"]["page"]
    assert page["data"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_list_capabilities_never_names_a_service(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_list_capabilities", "arguments": {}},
        name="lucy_list_capabilities",
    )
    assert response.status_code == 200, response.text
    body = response.text.lower()
    assert "spotify-api" not in body
    data = response.json()["result"]["structuredContent"]["data"]
    ids = {item["id"] for item in data}
    assert "music" in ids
    assert "help" in ids


@pytest.mark.asyncio
async def test_connect_music_does_not_name_the_provider_in_the_result(
    client: AsyncClient,
) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_connect", "arguments": {"capability": "music"}},
        name="lucy_connect",
    )
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    text = str(payload).lower()
    assert "spotify" not in text
    # The in-process keyring has no delegated authorize route, so this is the
    # transient-outage shape: listed, isError, actionable, no vanished tool.
    assert payload["isError"] is True


@pytest.mark.asyncio
async def test_unknown_tools_are_self_correcting_errors(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "stale_tool", "arguments": {}},
        name="stale_tool",
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "tools/list" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_legacy_tools_list_works_without_mcp_method(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers={**bearer(), "MCP-Protocol-Version": LEGACY},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200, response.text
    assert "lucy_session_create" in [tool["name"] for tool in response.json()["result"]["tools"]]


@pytest.mark.asyncio
async def test_invalid_jsonrpc_is_an_invalid_request(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("server/discover"),
        json={"jsonrpc": "1.0", "id": 1, "method": "server/discover", "params": {"_meta": META}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_a_non_object_payload_is_an_invalid_request(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("server/discover"),
        json=[1, 2],
    )
    assert response.status_code == 400
    assert "id" not in response.json()


@pytest.mark.asyncio
async def test_undecodable_bytes_are_a_parse_error(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("server/discover"),
        content=b"\xff",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == PARSE


@pytest.mark.asyncio
async def test_an_unknown_notification_is_method_not_found(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=bearer(),
        json={"jsonrpc": "2.0", "method": "notifications/cancelled"},
    )
    assert response.status_code == 404
    assert "id" not in response.json()
    assert response.json()["error"]["code"] == UNKNOWN_METHOD


@pytest.mark.asyncio
async def test_tools_call_without_a_name_is_invalid(client: AsyncClient) -> None:
    response = await _rpc(client, "tools/call", {"arguments": {}}, name="lucy_chat")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_tools_call_arguments_must_be_an_object(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_chat", "arguments": ["hello"]},
        name="lucy_chat",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chat_without_text_is_invalid(client: AsyncClient) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_chat", "arguments": {"session_id": session_id}},
        name="lucy_chat",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_describe_and_run_plan_work_on_a_session(client: AsyncClient) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {"profile": "personal"}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]
    described = await _rpc(
        client,
        "tools/call",
        {"name": "describe_operations", "arguments": {"session_id": session_id}},
        name="describe_operations",
    )
    assert described.status_code == 200, described.text
    assert described.json()["result"]["isError"] is False
    planned = await _rpc(
        client,
        "tools/call",
        {"name": "run_plan", "arguments": {"session_id": session_id, "steps": []}},
        name="run_plan",
    )
    assert planned.status_code == 200, planned.text
    missing = await _rpc(
        client,
        "tools/call",
        {
            "name": "lucy_get_result",
            "arguments": {"session_id": session_id, "result_id": "missing"},
        },
        name="lucy_get_result",
    )
    assert missing.status_code == 200
    assert missing.json()["result"]["isError"] is True
    ref = await _rpc(
        client,
        "tools/call",
        {"name": "get_result", "arguments": {"session_id": session_id, "ref": "$nope"}},
        name="get_result",
    )
    assert ref.status_code == 200
    assert ref.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_unknown_capabilities_and_missing_session_are_self_correcting(
    client: AsyncClient,
) -> None:
    unknown = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_connect", "arguments": {"capability": "telepathy"}},
        name="lucy_connect",
    )
    assert unknown.json()["result"]["isError"] is True
    items = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_get_session_items", "arguments": {"session_id": "ses_missing"}},
        name="lucy_get_session_items",
    )
    assert items.json()["result"]["isError"] is True
    assert "expired" in items.json()["result"]["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_a_base64_name_header_is_accepted(client: AsyncClient) -> None:
    import base64

    encoded = base64.b64encode(b"lucy_list_capabilities").decode("ascii")
    response = await client.post(
        "/mcp",
        headers=_headers("tools/call", name=f"=?base64?{encoded}?="),
        json=_body(
            "tools/call",
            {"name": "lucy_list_capabilities", "arguments": {}},
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is False


@pytest.mark.asyncio
async def test_a_workspace_outage_is_an_error_not_a_session(client: AsyncClient) -> None:
    from lucy_api.clients.environments import FakeEnvironmentsClient
    from lucy_api.clients.errors import DownstreamError

    app = client._transport.app  # type: ignore[attr-defined]

    class Boom(FakeEnvironmentsClient):
        async def environments(self, *, profile: str = "") -> tuple[object, ...]:
            raise DownstreamError("environments", 503, "down")

    app.state.container.environment_override = Boom()
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "workspace" in response.json()["result"]["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_a_non_absent_domain_error_is_surfaced_as_is_error(client: AsyncClient) -> None:
    from lucy_api.core.errors import LucyError

    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]
    app = client._transport.app  # type: ignore[attr-defined]
    original = app.state.container.store.get

    async def busy(account: str, session: str) -> dict[str, object]:
        if session == session_id:
            raise LucyError("conflict", "The session is busy.", 409)
        return await original(account, session)

    app.state.container.store.get = busy
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_get_session_items", "arguments": {"session_id": session_id}},
        name="lucy_get_session_items",
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert "busy" in response.json()["result"]["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_connect_returns_a_lucy_origin_url(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lucy_api.clients.keyring import FakeKeyringClient
    from lucy_api.clients.spotify import SERVICE as MUSIC_SERVICE
    from lucy_api.core.container import Container

    fake = FakeKeyringClient()
    monkeypatch.setattr(Container, "connection_client", lambda self, _request: fake)
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_connect", "arguments": {"capability": "music"}},
        name="lucy_connect",
    )
    assert response.status_code == 200, response.text
    payload = response.json()["result"]
    assert payload["isError"] is False
    structured = payload["structuredContent"]
    assert structured["ticket"]
    assert structured["connect_url"].startswith("http://")
    assert "spotify" not in str(payload).lower()
    assert fake.authorized == [("personal", MUSIC_SERVICE)]


@pytest.mark.asyncio
async def test_run_plan_without_steps_is_invalid(client: AsyncClient) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_run_plan", "arguments": {"session_id": session_id}},
        name="lucy_run_plan",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_omitted_tool_arguments_are_an_empty_object(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_list_capabilities"},
        name="lucy_list_capabilities",
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


@pytest.mark.asyncio
async def test_describe_without_a_session_still_lists_operations(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "describe_operations", "arguments": {}},
        name="describe_operations",
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False


@pytest.mark.asyncio
async def test_get_result_without_a_ref_is_invalid(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "get_result", "arguments": {"session_id": "ses_x"}},
        name="get_result",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_an_absent_domain_error_from_a_tool_looks_expired(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lucy_api.core.errors import absent
    from lucy_api.mcp import handlers

    async def boom(_call: object, _arguments: object) -> dict[str, object]:
        raise absent()

    monkeypatch.setitem(handlers._TOOLS, "lucy_list_capabilities", boom)
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_list_capabilities", "arguments": {}},
        name="lucy_list_capabilities",
    )
    assert response.status_code == 200
    assert "expired" in response.json()["result"]["content"][0]["text"].lower()


@pytest.mark.asyncio
async def test_an_empty_method_is_an_invalid_request(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("server/discover"),
        json={"jsonrpc": "2.0", "id": 1, "method": "", "params": {"_meta": META}},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_legacy_tools_call_without_a_name_is_invalid(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers={**bearer(), "MCP-Protocol-Version": LEGACY},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_name_header_mismatch_does_not_echo_the_names(client: AsyncClient) -> None:
    response = await client.post(
        "/mcp",
        headers=_headers("tools/call", name="lucy_chat"),
        json=_body("tools/call", {"name": "lucy_list_capabilities", "arguments": {}}),
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == HEADER_MISMATCH
    assert "lucy_chat" not in error["message"]
    assert "lucy_list_capabilities" not in error["message"]


@pytest.mark.asyncio
async def test_chat_without_a_session_id_is_invalid(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_chat", "arguments": {"session_id": "", "text": "hello"}},
        name="lucy_chat",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_lucy_get_result_without_an_id_is_invalid(client: AsyncClient) -> None:
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_get_result", "arguments": {"session_id": "ses_x"}},
        name="lucy_get_result",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_turn_that_is_already_running_is_still_returned(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]

    async def running(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"id": "turn_running", "status": "running"}

    monkeypatch.setattr("lucy_api.mcp.handlers.submit_messages", running)
    response = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_chat", "arguments": {"session_id": session_id, "text": "hello"}},
        name="lucy_chat",
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["status"] == "running"


@pytest.mark.asyncio
async def test_stored_results_are_returned_when_they_exist(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await _rpc(
        client,
        "tools/call",
        {"name": "lucy_session_create", "arguments": {}},
        name="lucy_session_create",
    )
    session_id = created.json()["result"]["structuredContent"]["session_id"]

    async def resolved(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"ref": "$hits", "data": [{"title": "One"}]}

    async def stored(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"id": "hits", "data": [{"title": "One"}]}

    monkeypatch.setattr("lucy_api.mcp.handlers.resolve_result", resolved)
    monkeypatch.setattr("lucy_api.mcp.handlers.get_result", stored)
    by_ref = await _rpc(
        client,
        "tools/call",
        {"name": "get_result", "arguments": {"session_id": session_id, "ref": "$hits"}},
        name="get_result",
    )
    assert by_ref.status_code == 200, by_ref.text
    assert by_ref.json()["result"]["isError"] is False
    assert by_ref.json()["result"]["structuredContent"]["result"]["ref"] == "$hits"
    by_id = await _rpc(
        client,
        "tools/call",
        {
            "name": "lucy_get_result",
            "arguments": {"session_id": session_id, "result_id": "hits"},
        },
        name="lucy_get_result",
    )
    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["result"]["structuredContent"]["result"]["id"] == "hits"
