"""Header-to-body checks the Streamable HTTP binding requires.

Mismatched headers are how a load balancer and the process behind it can be talked
into disagreeing about which tool ran. The refusal never copies the two disagreeing
values: those are caller-controlled strings and a 400 body is a log line.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Any

from lucy_api.mcp.protocol import (
    CURRENT,
    LEGACY,
    META_VERSION,
    header_mismatch,
    missing_meta,
    unsupported_version,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

BASE64_PREFIX = "=?base64?"
BASE64_SUFFIX = "?="


def lookup(headers: Mapping[str, str], name: str) -> str:
    """Read a header without caring how the client capitalised it."""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return ""


def decode_mirrored(value: str) -> str:
    """Undo the spec's Base64 sentinel so a comparison is against the body string."""
    if not (value.startswith(BASE64_PREFIX) and value.endswith(BASE64_SUFFIX)):
        return value
    raw = value[len(BASE64_PREFIX) : -len(BASE64_SUFFIX)]
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise header_mismatch() from exc


def protocol_version(headers: Mapping[str, str], payload: dict[str, Any]) -> str:
    """The version this request is speaking, or a JSON-RPC error explaining why not."""
    method = str(payload.get("method") or "")
    header = lookup(headers, "MCP-Protocol-Version")
    if method == "initialize":
        return LEGACY
    if not header:
        raise header_mismatch()
    if header not in {CURRENT, LEGACY}:
        raise unsupported_version(header)
    if header == LEGACY:
        return LEGACY
    meta = _meta(payload)
    if meta is None:
        raise missing_meta()
    declared = meta.get(META_VERSION)
    if not isinstance(declared, str) or not declared:
        raise missing_meta()
    if declared != header:
        raise header_mismatch()
    return CURRENT


def assert_method_header(headers: Mapping[str, str], method: str, version: str) -> None:
    """Modern requests must mirror ``method``; legacy ones were not asked to."""
    if version != CURRENT:
        return
    mirrored = lookup(headers, "Mcp-Method")
    if mirrored != method:
        raise header_mismatch()


def assert_name_header(headers: Mapping[str, str], payload: dict[str, Any], version: str) -> None:
    """``tools/call`` mirrors ``params.name``. Other methods have nothing to mirror."""
    if version != CURRENT or payload.get("method") != "tools/call":
        return
    params = payload.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if not isinstance(name, str):
        raise header_mismatch()
    mirrored = decode_mirrored(lookup(headers, "Mcp-Name"))
    if mirrored != name:
        raise header_mismatch()


def _meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    return meta
