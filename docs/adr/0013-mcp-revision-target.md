# ADR-0013: Lucy is a dual-era MCP server

**Status:** accepted

## Context

MCP revision 2026-07-28 deletes the initialize handshake, protocol sessions and
`Mcp-Session-Id`, and requires `server/discover` with per-request `_meta`. Desktop
clients in circulation still speak 2025-11-25. A modern-only hub would fail for
every one of them.

The Python SDK (`mcp>=2.2`) implements both revisions, but it wants to own the
HTTP stack. Lucy already owns FastAPI, bearer verification, problem+json and the
session store. A second Starlette app beside that is two opinions about Origin
and 401.

## Decision

Lucy targets **2026-07-28** and keeps **2025-11-25 as a dual-era path**.
`server/discover` advertises `supportedVersions: ["2026-07-28","2025-11-25"]`.
Modern requests are served statelessly on `POST /mcp`. `initialize` selects
legacy semantics for that request. GET and DELETE are 405. `Mcp-Session-Id` is
ignored and never echoed.

The wire protocol is implemented in `lucy_api.mcp`, not by mounting the SDK.
weftai's three MCP tools (`run_plan`, `describe_operations`, `get_result`) are
exposed with weftai's own descriptions. Lucy's session tools sit beside them.

Egress for Lucy-as-client stays in `lucy_api.net.ssrf`. Hash-pinned external
servers remain a later slice of this milestone.

## Consequences

Legacy clients work. Modern clients never see a protocol session. Header/body
mismatches are 400 with code -32020 and a message that does not echo the two
disagreeing values. A 401 carries `WWW-Authenticate` with RFC 9728
`resource_metadata`.

We would change this if the SDK grew a FastAPI adapter that reused our auth and
error handlers, or if 2025-11-25 traffic disappeared.
