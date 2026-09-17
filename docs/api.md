# The hub's HTTP surface

Everything authenticated lives under `/v1`. Authentication is
`Authorization: Bearer <keyring JWT>` whose `aud` is exactly `lucy-api`, verified locally
against keyring's published keys. There is no API key, no session cookie, and no parameter
that names an account.

| Method | Path | Auth | What it is for |
| --- | --- | --- | --- |
| `GET` | `/healthy` | none | Liveness. No I/O, never fails. Point a container healthcheck here. |
| `GET` | `/ready` | none | Readiness. Reports each dependency and answers 503 when one is unusable. Point a load balancer here. |
| `GET` | `/v1/me` | bearer | The account the presented token is for. The first call a client makes. |

## Failure shapes

| Status | When | What to do |
| --- | --- | --- |
| 401 | The token is missing, malformed, expired, or minted for another audience | Get a new token. Do not retry the same one. |
| 503 + `Retry-After: 5` | Keyring's keys could not be fetched | Retry. Your token is probably fine; this is not your problem. |

The two are deliberately different. Telling somebody to log in again because keyring
blipped is advice that does not help.

A body never echoes the value that was refused: a 4xx body is logged by the caller and
handed back to a model, and a value rejected for looking like a credential must not then be
copied into a log line.

## What is coming

The surface grows along the milestones in the plan: sessions, turns and items with one
write path (`POST /v1/sessions/{id}/inputs`), a resumable SSE event stream, capabilities
and connections, the workspace, memory, sub-agents, approvals, and an MCP server at
`POST /mcp`. Each lands with its own section here.
