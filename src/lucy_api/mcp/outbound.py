"""Fetch a remote tools/list after the destination has already been allowed.

Redirects are refused: a 30x Location is another URL, and following it without running
the same SSRF check is how an allowlisted host becomes 169.254.169.254. The caller maps
transport failures onto a fixed sentence that never includes the URL.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from lucy_api.context.scrub import fence
from lucy_api.core.errors import LucyError
from lucy_api.mcp.protocol import LEGACY

UNREACHABLE = "That server could not be listed; check the URL and try again."
BAD_LISTING = "That server did not return a tools list Lucy can pin."
CALL_FAILED = "That tool could not run."
UNREACHABLE_CODE = "mcp-unreachable"
BAD_LISTING_CODE = "mcp-bad-listing"
MAX_RESULT_CHARS = 4_000


class Listing(Protocol):
    async def __call__(self, url: str) -> list[object]: ...


class CallTool(Protocol):
    async def __call__(
        self, url: str, name: str, arguments: dict[str, object]
    ) -> dict[str, object]: ...


def unreachable() -> LucyError:
    return LucyError(UNREACHABLE_CODE, UNREACHABLE, 503)


def bad_listing() -> LucyError:
    return LucyError(BAD_LISTING_CODE, BAD_LISTING, 400)


def httpx_listing(client: httpx.AsyncClient) -> Listing:
    """POST tools/list on the already-checked URL. Never follows a redirect."""

    async def list_tools(url: str) -> list[object]:
        try:
            response = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"MCP-Protocol-Version": LEGACY, "Content-Type": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise unreachable() from exc
        if not response.is_success:
            raise unreachable()
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise bad_listing() from exc
        if not isinstance(payload, dict):
            raise bad_listing()
        result = payload.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise bad_listing()
        return tools

    return list_tools


def httpx_call(client: httpx.AsyncClient) -> CallTool:
    """POST tools/call on an already-checked URL. Never follows a redirect."""

    async def call_tool(url: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        try:
            response = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers={"MCP-Protocol-Version": LEGACY, "Content-Type": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise unreachable() from exc
        if not response.is_success:
            raise unreachable()
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise bad_listing() from exc
        return project_call(payload)

    return call_tool


def project_call(payload: object) -> dict[str, object]:
    """Text-only, fenced, length-capped. A cut names how much was kept."""
    if not isinstance(payload, dict) or payload.get("error") is not None:
        return {"ok": False, "text": CALL_FAILED}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "text": CALL_FAILED}
    content = result.get("content")
    items = content if isinstance(content, list) else []
    pieces = [
        fence(str(item.get("text") or ""))
        for item in items
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    failed = bool(result.get("isError"))
    joined = "\n".join(pieces)
    text = joined[:MAX_RESULT_CHARS]
    if failed and not text:
        text = CALL_FAILED
    projected: dict[str, object] = {"ok": not failed, "text": text}
    if len(joined) > MAX_RESULT_CHARS:
        projected["notice"] = f"showing {len(text)} of {len(joined)} characters"
    return projected
