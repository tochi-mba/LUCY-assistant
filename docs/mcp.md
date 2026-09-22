# MCP

Lucy is an MCP server and an MCP client. This page is both.

The transport is Streamable HTTP on `POST /mcp` only. Revision **2026-07-28** is
the default. `server/discover` also names **2025-11-25**, and an `initialize`
request selects that legacy handshake for that call. There is no protocol
session: a `Mcp-Session-Id` header is ignored and never echoed. GET and DELETE
are 405.

Conversation state is a Lucy session, not an MCP session. `lucy_session_create`
returns an opaque `session_id` in `structuredContent`. Every other Lucy tool
takes that id. The store keys it to the verified token subject, so a handle
from another account looks expired. The error text is always the same sentence
telling the model to create a new one.

## Auth

Every POST needs `Authorization: Bearer` with `aud=lucy-api`. A 401 includes

```
WWW-Authenticate: Bearer resource_metadata="http://127.0.0.1:8000/.well-known/oauth-protected-resource", scope="lucy-api"
```

The well-known document is unauthenticated, per RFC 9728. Origin, when present,
must be this process; a mismatch is 403 with a body that does not name the
origin. CLI clients that omit Origin are allowed.

## Tools

The list is a function of the presented token, never of connection history.
`tools/list` is `cacheScope: "private"` with a two-minute TTL. `server/discover`
is public with a one-hour TTL. A stale tool name returns `isError: true` telling
the model to call `tools/list` again.

Lucy tools: `lucy_session_create`, `lucy_chat`, `lucy_list_capabilities`,
`lucy_connect`, `lucy_get_session_items`, `lucy_run_plan`, `lucy_get_result`.

weftai tools, descriptions unpatched: `run_plan`, `describe_operations`,
`get_result`.

`lucy_connect` takes a capability id (`music`), never a service name. The
connect URL is on Lucy's origin.

Reads carry `readOnlyHint`. Nothing irreversible is advertised as safe.
Unannotated tools would default to destructive and open-world; every tool here
is annotated.

## Skills

`skills/list` and `skills/get` serve Lucy's own procedures: talking,
capabilities, music, research, workspace, notes, settings, helpers, approvals,
memory, and external tools. Each entry has a SHA-256 digest and a byte size. Approval of a skill is approval of those bytes;
a change is a new digest. `resources/read` on a `skill://lucy/<name>` URI is the
same document. The list is `cacheScope: public` with a one-hour TTL. Capability
names only — never a service, a port or an HTTP verb.

## Tasks

`tasks/list`, `tasks/get` and `tasks/cancel` project the work registry for one
Lucy session. They exist only when **this request** names the
`io.modelcontextprotocol/tasks` extension in client capabilities (there is no
protocol session to remember a handshake). A client that did not opt in gets
the same `-32601` as any other unknown method. Lucy never returns a task handle
on the default synchronous `lucy_chat` path.

## Errors

JSON-RPC failures use the spec's HTTP statuses and codes (`-32602` missing
`_meta`, `-32022` unsupported version with `data.supported`, `-32601` unknown
method as 404, `-32020` header mismatch). Those bodies are JSON-RPC, not
problem+json. Origin 403 and bearer 401 stay problem+json, like the rest of the
hub.

Header mismatch messages never copy the two disagreeing values.

## Lucy as a client

A person registers an HTTPS server at `/v1/mcp/servers`. Lucy POSTs `tools/list`
with the 2025-11-25 protocol header, fences every description, caps the listing
at 64 tools, and stores a SHA-256 digest. A later listing that does not match
is `pin_mismatch`: the stored tools stay, they are not offered, and nothing is
silently adopted. Loopback and RFC 1918 destinations are the same 403 as every
other refused URL.

Ready servers become `mcp.<server>.<tool>` in the model registry. The wire name
is the server's original tool name. Results are text-only, fenced, and capped.
Every such call is a write under `mcp.invoke`.
