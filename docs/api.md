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
| `GET` | `/v1/setup` | bearer | Deployment readiness and setup guidance for each capability. |
| `POST` | `/v1/auth/device` | none | Start passwordless CLI sign-in. |
| `POST` | `/v1/auth/device/token` | none | Poll a device code using RFC 8628 error words. |
| `POST` | `/v1/auth/device/authorize` | bearer | Approve or deny a device code from an existing signed-in client. |
| `GET` | `/v1/connections` | bearer | Provider status, granted scopes and expiry; never credentials. |
| `POST` | `/v1/connections/{service}/authorize` | bearer | Create a subject-bound Lucy-origin consent link. |
| `GET` | `/v1/connections/{service}/authorize/{ticket}` | bearer | Poll provider connection state. |
| `DELETE` | `/v1/connections/{service}` | bearer | Idempotently disconnect a provider. |
| `GET` | `/.well-known/oauth-protected-resource` | none | RFC 9728 resource metadata. Where tokens for this API come from. |
| `POST` | `/mcp` | bearer | JSON-RPC MCP. 2026-07-28 with a 2025-11-25 initialize path. GET and DELETE are 405. |
| `GET` | `/v1/mcp/servers` | bearer | External MCP servers this account pinned. |
| `POST` | `/v1/mcp/servers` | bearer | Fetch tools/list over HTTPS, hash-pin, store. Loopback is 403. |
| `GET` | `/v1/mcp/servers/{name}` | bearer | One pinned server. Cross-account is 404. |
| `POST` | `/v1/mcp/servers/{name}/refresh` | bearer | Re-list and compare the digest. A change is `pin_mismatch`, not a silent update. |
| `DELETE` | `/v1/mcp/servers/{name}` | bearer | Forget a pinned server. |
| `GET` | `/v1/sessions/{id}/memory` | bearer | Trusted topic index currently eligible for this conversation. Incognito is `[]`. |
| `GET` | `/v1/sessions/{id}/workspace` | bearer | Attached environment id and session-relative path. Never a host path. |
| `POST` | `/v1/sessions/{id}/workspace` | bearer | Attach if missing. Idempotent when already attached. |
| `POST` | `/v1/sessions/{id}/workspace/reset` | bearer | Wipe the session subtree and seed `progress.md` / `tasks.json` again. |
| `GET` | `/v1/sessions/{id}/subagents` | bearer | Durable helper roster for this conversation. |
| `GET` | `/v1/sessions/{id}/subagents/{id}` | bearer | One helper. A stranger's id is 404. |
| `GET` | `/v1/sessions/{id}/subagents/{id}/items` | bearer | The helper's own item log. Parent items are not here. |
| `GET` | `/v1/sessions/{id}/subagents/{id}/turns` | bearer | Always an empty page: helpers share the parent's turn row. |
| `POST` | `/v1/webhooks` | bearer | Register an HTTPS signal destination. Secret is in this response only. |
| `GET` | `/v1/webhooks` | bearer | Destinations this account registered. Secrets are never listed. |
| `DELETE` | `/v1/webhooks/{id}` | bearer | Stop signalling a destination. Cross-account is 404. |

`/v1/setup` returns `{account_id, services}`. Each service has a stable `id`, `title`,
`required`, deployment `state` (`ready`, `degraded`, `unavailable`), `connection_state`
(`not_required`, `unknown`, `connected`, `disconnected`, `pending`), `summary`, sanitized
`checks`, and `actions` describing operator steps and documentation links. Optional outages
do not suppress other rows. Deployment probes use public readiness routes and never forward
the caller's JWT as a bearer. Per-account connection inspection uses Keyring's internal
two-credential boundary: Lucy's service token in `Authorization` and the signed caller
token only as subject proof. `unknown` means that inspect failed this request, not that
Lucy cannot inspect connections. Notes use Memory-api the same way, on
`/v1/internal/memory`, never the person-facing `/v1/memory` routes a model is given. Pinned
account fields are a separate standing feed and a separate `notes.aboutMe.account` list;
`notes.search` does not query User-api. The manifests currently live in the hub's
onboarding adapters, based on each service's own documentation.

Device-token failures deliberately use OAuth's `{error, error_description}` body rather
than the normal problem document because RFC 8628 clients branch on `authorization_pending`,
`slow_down`, `access_denied`, and `expired_token`. Successful codes are one-time and the
database prunes expired codes.

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

Session, turn and item endpoints use the one write path at
`POST /v1/sessions/{id}/inputs`; streams are resumable. Creating a session provisions its
dedicated `sessions/<id>` directory in the account/profile environment before returning
`201`. A retry with the same idempotency key recovers or reuses that directory, and a fork
always receives a fresh directory rather than sharing its parent's files. A provisioning outage returns
`503 workspace-unavailable` and is safe to retry.

`input.message` starts a turn. `input.approval` answers a parked write: Lucy records a
grant (once, this session, this profile, or the whole account) and re-queues the turn.
The client's `approved: true` is an input, not an authorization — the gate re-checks the
ledger before the tool runs. A denial is a transcript item plus a grant the model will
see as "not allowed", never an exception.

`GET /v1/permissions?profile=personal` lists every permission declared by an installed
capability and its effective grant, if any. Profile grants override account-wide (`*`)
grants. `PUT /v1/permissions` records a decision. `DELETE /v1/permissions/{id}` returns
that permission to "not yet asked". The bearer token always selects the account, so one
person cannot name or inspect another person's grants.

`GET /v1/tools` is the bound registry for this turn — names the model can actually call,
after deferred loading. `POST /v1/tools/{name}/invoke` runs one of those operations without
starting a model turn. The gate is the same: a write that still needs a person is `409`.
Grant it through `/v1/permissions` or answer `input.approval` on the session, then retry.
A plan that asks for two writes parks both; answering one leaves the other pending.

Every grant, refusal, ask, revoke and auto-mode bypass is an append-only `audit` row. The
row names the permission and never the arguments.

The remaining planned verification is family-wide `make up` against real siblings.
Files, artifacts, account erasure, session projections and live-turn streaming are on this
hub:

- `POST /v1/files` (idempotent) stores an upload against the account.
- `GET /v1/files`, `GET /v1/files/{id}`, `GET /v1/files/{id}/content`, `DELETE /v1/files/{id}`.
- `GET /v1/sessions/{id}/artifacts`, `GET /v1/artifacts/{id}`, `GET /v1/artifacts/{id}/content`.
- `DELETE /v1/account` wipes Lucy's copy of the person (sessions, files, grants, MCP pins, webhooks). Memory-api is not this store.
- `GET /v1/sessions/{id}/events` already negotiates the AI-SDK UI stream; a live turn now writes `lucy.content.text.*` and `lucy.content.reasoning.*` into that log.
- `GET /v1/sessions/{id}/memory` is a projection of Memory-api's topic index, not a second store. CRUD stays on Memory-api; the hub never names an account on those calls.
- `POST /v1/webhooks` posts `{session_id, turn_id, status}` signed as `X-Lucy-Signature: sha256=…`. Loopback and RFC 1918 destinations are 403. The body is never a transcript.

External MCP servers are registered at `/v1/mcp/servers` and appear to the model
as `mcp.<server>.<tool>`. The MCP server is `POST /mcp`.
