"""Execute one MCP tool against the hub's existing session, pack and result code.

Tools never see a sibling service name. ``lucy_connect`` takes a capability id; the
keyring service it maps to stays inside this module.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from lucy_api import __version__
from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.spotify import SERVICE as MUSIC_SERVICE
from lucy_api.core.container import PackRequest
from lucy_api.core.errors import LucyError
from lucy_api.mcp.catalog import cache_hint, listed
from lucy_api.mcp.protocol import (
    DISCOVER_TTL_MS,
    GONE,
    INSTRUCTIONS,
    LEGACY,
    META_SERVER,
    SERVER_CAPABILITIES,
    SUPPORTED,
    invalid_request,
    server_info,
)
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportDownstreamError
from lucy_api.sessions.items import list_items
from lucy_api.sessions.models import CreateSession, Cursor
from lucy_api.sessions.results import get_result, resolve_result
from lucy_api.sessions.turns import submit_messages

if TYPE_CHECKING:
    from lucy_api.api.dependencies import ActingAs
    from lucy_api.core.container import Container

CONNECTABLE = {"music": MUSIC_SERVICE}
WORKSPACE_UNAVAILABLE = (
    "The session workspace could not be provisioned; retry when it is available."
)
CONNECT_FAILED = "That capability could not be connected; try again when the vault is healthy."
UNKNOWN_CAPABILITY = "Unknown capability. Call lucy_list_capabilities and use an id from there."
UNKNOWN_TOOL = "Unknown tool. Call tools/list and use a name from the current list."
NEED_TEXT = "lucy_chat needs a session_id and text."
NEED_SESSION = "This tool needs a session_id from lucy_session_create."
NEED_STEPS = "run_plan needs a steps array."
NEED_REF = "get_result needs a $ref."
NEED_RESULT = "lucy_get_result needs a result_id."


class SessionGoneError(Exception):
    """The handle is missing or belongs to somebody else. Same sentence either way."""


@dataclass(frozen=True, slots=True)
class McpCall:
    acting: ActingAs
    container: Container


def _pack(call: McpCall, *, profile: str, session_id: str) -> PackRequest:
    return PackRequest(
        caller=call.acting.caller,
        user_token=call.acting.token,
        profile=profile or "personal",
        session_id=session_id,
    )


def _origin(call: McpCall) -> str:
    settings = call.container.settings
    return f"http://{settings.host}:{settings.port}"


def tool_result(
    text: str, structured: dict[str, Any] | None = None, *, error: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": error,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


def gone() -> dict[str, Any]:
    return tool_result(GONE, error=True)


async def owned_session(call: McpCall, session_id: str) -> dict[str, Any]:
    if not session_id:
        raise invalid_request(NEED_SESSION)
    try:
        return await call.container.store.get(call.acting.account_id, session_id)
    except LucyError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            raise SessionGoneError from exc
        raise


def discover_result(call: McpCall) -> dict[str, Any]:
    info = server_info(call.container.settings.app_name, __version__)
    return {
        "supportedVersions": list(SUPPORTED),
        "capabilities": SERVER_CAPABILITIES,
        "instructions": INSTRUCTIONS,
        "ttlMs": DISCOVER_TTL_MS,
        "cacheScope": "public",
        "_meta": {META_SERVER: info},
    }


def initialize_result(call: McpCall) -> dict[str, Any]:
    info = server_info(call.container.settings.app_name, __version__)
    return {
        "protocolVersion": LEGACY,
        "capabilities": SERVER_CAPABILITIES,
        "serverInfo": info,
        "instructions": INSTRUCTIONS,
    }


async def describe_bound(call: McpCall, session_id: str, profile: str) -> str:
    context = call.container.pack_context(_pack(call, profile=profile, session_id=session_id))
    catalogue = await call.container.capabilities.probe(context)
    return call.container.capabilities.registry_for(catalogue, session_id).describe()


async def list_tools(call: McpCall, session_id: str, profile: str) -> dict[str, Any]:
    description = await describe_bound(call, session_id, profile)
    return {"tools": listed(description), **cache_hint()}


async def call_tool(call: McpCall, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _call(call, name, arguments)
    except SessionGoneError:
        return gone()
    except LucyError as exc:
        if exc.status == HTTPStatus.NOT_FOUND:
            return gone()
        return tool_result(str(exc), error=True)


async def create_session(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    title = arguments.get("title")
    profile = str(arguments.get("profile") or "personal")
    request = CreateSession(profile=profile)
    if isinstance(title, str) and title:
        request = CreateSession(profile=profile, title=title)
    key = secrets.token_urlsafe(16)
    row = await call.container.store.create(call.acting.account_id, request, key)
    try:
        row = await call.container.ensure_workspace(
            _pack(call, profile=profile, session_id=str(row["id"])), row
        )
    except (
        DownstreamError,
        ExchangeError,
        NoBrokerError,
        TransportDownstreamError,
        LucyError,
    ):
        return tool_result(WORKSPACE_UNAVAILABLE, error=True)
    session_id = str(row["id"])
    return tool_result(
        f"Created session {session_id}.",
        {"session_id": session_id},
    )


async def chat(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    text = arguments.get("text")
    if not isinstance(text, str) or not text:
        raise invalid_request(NEED_TEXT)
    session = await owned_session(call, session_id)
    prepared = await call.container.prepare_turn(
        _pack(
            call,
            profile=str(session["profile"]),
            session_id=session_id,
        ),
        session,
    )
    events = [{"type": "input.message", "content": text}]
    row = await submit_messages(
        call.container.store,
        call.acting.account_id,
        session_id,
        events,
        secrets.token_urlsafe(16),
    )
    if row["status"] == "queued":
        call.container.turns.authorize(str(row["id"]), prepared)
    await call.container.events.publish_persisted(session_id)
    call.container.turns.wake()
    turn_id = str(row["id"])
    return tool_result(f"Queued turn {turn_id}.", {"turn_id": turn_id, "status": row["status"]})


async def capabilities(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    profile = str(arguments.get("profile") or "personal")
    context = call.container.pack_context(_pack(call, profile=profile, session_id=""))
    catalogue = await call.container.capabilities.probe(context)
    data = call.container.capabilities.listings(catalogue)
    return tool_result(f"{len(data)} capabilities.", {"data": data})


async def connect(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    capability = str(arguments.get("capability") or "")
    service = CONNECTABLE.get(capability)
    if service is None:
        return tool_result(UNKNOWN_CAPABILITY, error=True)
    profile = str(arguments.get("profile") or "personal")
    request = _pack(call, profile=profile, session_id="")
    try:
        authorization = await call.container.connection_client(request).authorize(profile, service)
    except (DownstreamError, ExchangeError, NoBrokerError, TransportDownstreamError):
        return tool_result(CONNECT_FAILED, error=True)
    ticket = call.container.connection_tickets.create(
        call.acting.account_id, profile, service, authorization
    )
    query = urlencode({"ticket": ticket.id})
    connect_url = f"{_origin(call)}/connect?{query}"
    return tool_result(
        "Open this URL to connect. The capability stays listed until that finishes.",
        {"connect_url": connect_url, "ticket": ticket.id},
    )


async def items(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    await owned_session(call, session_id)
    page = await list_items(call.container.store, call.acting.account_id, session_id, Cursor())
    return tool_result(f"{len(page['data'])} items.", {"page": page})


async def run_plan(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    steps = arguments.get("steps")
    if not isinstance(steps, list):
        raise invalid_request(NEED_STEPS)
    session = await owned_session(call, session_id)
    context = call.container.pack_context(
        _pack(call, profile=str(session["profile"]), session_id=session_id)
    )
    catalogue = await call.container.capabilities.probe(context)
    context.catalogue = catalogue
    result = await call.container.capabilities.execute({"steps": steps}, context)
    text = str(result.get("text") or "Plan finished.")
    return tool_result(text, {"plan": result})


async def describe(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    profile = "personal"
    if session_id:
        session = await owned_session(call, session_id)
        profile = str(session["profile"])
    text = await describe_bound(call, session_id, profile)
    return tool_result(text)


async def weftai_result(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    ref = arguments.get("ref")
    if not isinstance(ref, str) or not ref:
        raise invalid_request(NEED_REF)
    await owned_session(call, session_id)
    try:
        resolved = await resolve_result(
            call.container.store, call.acting.account_id, session_id, ref
        )
    except LucyError as exc:
        return tool_result(str(exc), error=True)
    return tool_result("Resolved reference.", {"result": resolved})


async def lucy_result(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    session_id = str(arguments.get("session_id") or "")
    result_id = arguments.get("result_id")
    if not isinstance(result_id, str) or not result_id:
        raise invalid_request(NEED_RESULT)
    await owned_session(call, session_id)
    try:
        stored = await get_result(
            call.container.store, call.acting.account_id, session_id, result_id
        )
    except LucyError as exc:
        return tool_result(str(exc), error=True)
    return tool_result("Stored result.", {"result": stored})


ToolHandler = Callable[[McpCall, dict[str, Any]], Awaitable[dict[str, Any]]]

_TOOLS: dict[str, ToolHandler] = {
    "lucy_session_create": create_session,
    "lucy_chat": chat,
    "lucy_list_capabilities": capabilities,
    "lucy_connect": connect,
    "lucy_get_session_items": items,
    "lucy_run_plan": run_plan,
    "run_plan": run_plan,
    "describe_operations": describe,
    "get_result": weftai_result,
    "lucy_get_result": lucy_result,
}


async def _call(call: McpCall, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = _TOOLS.get(name)
    if handler is None:
        return tool_result(UNKNOWN_TOOL, error=True)
    return await handler(call, arguments)
