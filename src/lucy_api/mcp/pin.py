"""Hash-pin untrusted MCP tool listings so a rug pull is a mismatch, not a silent adopt.

External tool descriptions are data. They are length-capped, scrubbed of harness markers,
and digested. Lucy stores the digest with the listing; a later fetch that does not match
is announced, and the pinned listing stays until a person accepts the change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lucy_api.context.scrub import fence

MAX_TOOLS = 64
MAX_NAME = 128
MAX_DESCRIPTION = 2_000


@dataclass(frozen=True, slots=True)
class PinnedTools:
    tools: tuple[dict[str, Any], ...]
    digest: str
    payload: str


def pin(raw: list[object]) -> PinnedTools:
    """Canonicalise a tools/list payload into something safe to store and compare."""
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or len(name) > MAX_NAME:
            continue
        description = fence(str(item.get("description") or ""))[:MAX_DESCRIPTION]
        schema = item.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object"}
        cleaned.append({"name": name, "description": description, "inputSchema": schema})
        if len(cleaned) >= MAX_TOOLS:
            break
    cleaned.sort(key=lambda tool: tool["name"])
    payload = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return PinnedTools(tuple(cleaned), digest, payload)
