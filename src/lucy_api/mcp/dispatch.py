"""Turn one JSON-RPC payload plus its mirrored headers into a response.

Shared so no handler can forget a check the Streamable HTTP binding requires: version,
method name, tool name, and the dual-era initialize path.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lucy_api.mcp.handlers import (
    McpCall,
    SessionGoneError,
    call_tool,
    discover_result,
    gone,
    initialize_result,
    list_tools,
)
from lucy_api.mcp.headers import assert_method_header, assert_name_header, protocol_version
from lucy_api.mcp.protocol import (
    RpcError,
    RpcResult,
    error_body,
    invalid_request,
    parse_error,
    result_body,
    unknown_method,
)
from lucy_api.mcp.skills import get_skill, read_resource
from lucy_api.mcp.skills import listed as list_skills
from lucy_api.mcp.tasks import handle as handle_tasks

if TYPE_CHECKING:
    from collections.abc import Mapping


async def handle(raw: bytes, headers: Mapping[str, str], call: McpCall) -> RpcResult:
    try:
        payload: object = json.loads(raw.decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        error = parse_error()
        return RpcResult(error.status, error_body(None, error, include_id=True))
    try:
        return await _dispatch(payload, headers, call)
    except SessionGoneError:
        rpc_id, _include = _rpc_id(payload if isinstance(payload, dict) else None)
        return RpcResult(200, result_body(rpc_id, gone()))
    except RpcError as error:
        rpc_id, include = _rpc_id(payload if isinstance(payload, dict) else None)
        return RpcResult(error.status, error_body(rpc_id, error, include_id=include))


async def _dispatch(payload: object, headers: Mapping[str, str], call: McpCall) -> RpcResult:
    if not isinstance(payload, dict):
        raise invalid_request()
    if payload.get("jsonrpc") != "2.0":
        raise invalid_request()
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise invalid_request()
    if "id" not in payload:
        return _notification(method)
    rpc_id = payload.get("id")
    if method == "initialize":
        return RpcResult(200, {"jsonrpc": "2.0", "id": rpc_id, "result": initialize_result(call)})
    version = protocol_version(headers, payload)
    assert_method_header(headers, method, version)
    assert_name_header(headers, payload, version)
    params = payload.get("params")
    arguments: dict[str, Any] = params if isinstance(params, dict) else {}
    result = await _invoke(method, arguments, call)
    return RpcResult(200, result_body(rpc_id, result))


async def _invoke(method: str, arguments: dict[str, Any], call: McpCall) -> dict[str, Any]:
    if method == "server/discover":
        return discover_result(call)
    if method == "tools/list":
        return await list_tools(
            call,
            str(arguments.get("session_id") or ""),
            str(arguments.get("profile") or "personal"),
        )
    if method == "tools/call":
        return await _call(arguments, call)
    if method in {"skills/list", "skills/get", "resources/read"}:
        return _skill_method(method, arguments)
    if method.startswith("tasks/"):
        return await handle_tasks(method, arguments, call)
    raise unknown_method()


async def _call(arguments: dict[str, Any], call: McpCall) -> dict[str, Any]:
    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        raise invalid_request()
    tool_args = arguments.get("arguments")
    if tool_args is None:
        tool_args = {}
    if not isinstance(tool_args, dict):
        raise invalid_request()
    return await call_tool(call, name, tool_args)


def _skill_method(method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if method == "skills/list":
        return list_skills()
    if method == "skills/get":
        return get_skill(arguments)
    return read_resource(arguments)


def _notification(method: str) -> RpcResult:
    if method == "notifications/initialized":
        return RpcResult(202, None)
    raise unknown_method()


def _rpc_id(payload: dict[str, Any] | None) -> tuple[object | None, bool]:
    if payload is None or "id" not in payload:
        return None, False
    return payload.get("id"), True


def bearer_challenge(host: str, port: int, audience: str) -> str:
    resource = f"http://{host}:{port}"
    metadata = f"{resource}/.well-known/oauth-protected-resource"
    return f'Bearer resource_metadata="{metadata}", scope="{audience}"'
