"""JSON-RPC envelopes and the two protocol revisions Lucy serves.

Modern requests (2026-07-28) carry version and client identity in ``params._meta``.
Legacy clients still open with ``initialize``; that handshake selects 2025-11-25
semantics for that request only. There is no protocol session to hang later calls on:
``Mcp-Session-Id`` is ignored and never echoed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CURRENT = "2026-07-28"
LEGACY = "2025-11-25"
SUPPORTED = (CURRENT, LEGACY)

JSONRPC = "2.0"
PARSE = -32700
INVALID = -32600
UNKNOWN_METHOD = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"

DISCOVER_TTL_MS = 3_600_000
TOOLS_TTL_MS = 120_000
GONE = "This session handle has expired. Create a new one with lucy_session_create."
HEADER_MISMATCH_DETAIL = "A request header does not match the JSON-RPC body."
MISSING_META = "Request is missing required _meta."
UNSUPPORTED_DETAIL = "Unsupported protocol version"
ORIGIN_REFUSED = "This origin is not allowed."
INSTRUCTIONS = (
    "Lucy is a conversation hub. Capabilities are named the way a person would name "
    "them: music, research, workspace, notes, settings, work, and helpers. Start with "
    "lucy_session_create, then lucy_chat or lucy_list_capabilities. An unconnected "
    "capability stays listed; lucy_connect is how the person links it. Plans use "
    "run_plan; stored results are read with get_result by $ref. Load a skill before "
    "calling tools you have not used: skills/list then skills/get, or resources/read "
    "on a skill://lucy/ URI."
)
TASKS_CAP = "io.modelcontextprotocol/tasks"
SERVER_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": True},
    "skills": {"listChanged": False},
    "resources": {"listChanged": False},
    "extensions": {TASKS_CAP: {}},
}


@dataclass(frozen=True, slots=True)
class RpcError(Exception):
    """A JSON-RPC failure with the HTTP status the Streamable HTTP binding requires."""

    code: int
    message: str
    status: int
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RpcResult:
    """Either a JSON-RPC body or an empty 202 for a notification the server accepted."""

    status: int
    body: dict[str, Any] | None


def server_info(name: str, version: str) -> dict[str, str]:
    return {"name": name, "version": version}


def result_body(rpc_id: object, result: dict[str, Any]) -> dict[str, Any]:
    payload = {"resultType": "complete", **result}
    return {"jsonrpc": JSONRPC, "id": rpc_id, "result": payload}


def error_body(rpc_id: object | None, error: RpcError, *, include_id: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.data is not None:
        payload["data"] = error.data
    body: dict[str, Any] = {"jsonrpc": JSONRPC, "error": payload}
    if include_id:
        body["id"] = rpc_id
    return body


def parse_error() -> RpcError:
    return RpcError(PARSE, "Parse error", 400)


def invalid_request(detail: str = "Invalid Request") -> RpcError:
    return RpcError(INVALID, detail, 400)


def missing_meta() -> RpcError:
    return RpcError(INVALID_PARAMS, MISSING_META, 400)


def header_mismatch() -> RpcError:
    return RpcError(HEADER_MISMATCH, HEADER_MISMATCH_DETAIL, 400)


def unknown_method() -> RpcError:
    return RpcError(UNKNOWN_METHOD, "Method not found", 404)


def unsupported_version(requested: str) -> RpcError:
    return RpcError(
        UNSUPPORTED_VERSION,
        UNSUPPORTED_DETAIL,
        400,
        {"supported": list(SUPPORTED), "requested": requested},
    )
