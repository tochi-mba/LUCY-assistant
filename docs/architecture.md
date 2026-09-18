# Architecture

The assistant is a hub and eight processes, not one. This document is the map. Each service's
own `docs/architecture.md` is the street view.

## Why eight repositories

A vault, a notes store, a headless browser, and a sandbox that runs arbitrary commands
do not share a release cycle, a threat model, or a dependency tree. Putting them in one
repository would make every change a change to all of them, and every deploy a deploy
of all of them. [ADR-0001](adr/0001-meta-repo-not-monorepo.md).

Shared behaviour that *must not* fork — verifying a Bearer token, resolving a person's
settings — lives as a client **inside the hub that owns the protocol**
([ADR-0002](adr/0002-shared-clients-live-in-the-hub.md)): `keyring_client` in
Keyring-api, `settings_client` in Settings-api. Consuming services depend on the
client; they do not vendor a copy.

## Identity

Keyring is the only service that knows a password, an OAuth grant, or an API key.

- People prove themselves to keyring with an **opaque session**.
- Services prove *who a request is for* with a **short-lived RS256 JWT** whose
  `aud` is that service's name. Verification is local, against
  `/.well-known/jwks.json`. [ADR-0003](adr/0003-bearer-canonical.md).
- A service that needs a credential presents **two** tokens on `/v1/internal`: its
  own shared secret and the user's JWT. Either alone is useless. That is the
  confused-deputy defence. Keyring's secret is the entry in
  `KEYRING_SERVICE_TOKENS`; Memory-api's is the entry in `MEMORY_SERVICE_TOKENS`;
  Settings-api's is the row in `SETTINGS_API_SERVICES`. Lucy holds matching
  copies as `LUCY_KEYRING_SERVICE_TOKEN`, `LUCY_MEMORY_API_TOKEN` and
  `LUCY_SETTINGS_API_TOKEN`.

User-api and Persona-api never call `/v1/internal`. They pin `iss` and `aud`
and fetch keys. They have no service token.

## Data, not the vault

| Service | What it stores | Encrypted? |
| --- | --- | --- |
| Keyring-api | Accounts, sessions, wrapped credentials | Credentials yes (envelope, `KEYRING_MASTER_KEY`) |
| User-api | Facts about the person | No. File mode 0600. |
| Persona-api | The assistant's notes about itself | No. File mode 0600. |
| Settings-api | Per-person choices | No. File mode 0600. |
| Web-search-api | In-memory jobs | Process lifetime only. |
| Spotify-api | In-memory jobs | Process lifetime only. |
| Environments-api | Workspace trees | On disk under `ENVAPI_ROOT`, sandboxed. |

Nothing that looks like a credential is accepted by user-api, persona-api, or
settings-api. That refusal is a test, not a comment.

## Settings versus configuration

Configuration (`KEYRING_PORT`, `SPOTIFY_API_HOST`) is a fact about the
machine and belongs in that service's env prefix. A fact about a **person**
(`spotify.default_market`, how long *their* artifacts are kept) belongs in
settings-api, namespaced, granted to the consuming service by
`SETTINGS_API_SERVICES`. A person cannot turn off SSRF protection, robots
compliance, or authentication from there.

Settings grants for credential consumers already exist so wiring a settings
client is a deployment choice, not a settings-api release.

## Ports

Family assignments are 8000–8009, and every repository's default now matches:
config, Dockerfile, `make run`, `.env.example` and documentation. Compose
publishes each port on the host and the image listens on the same number
inside. [ADR-0004](adr/0004-port-assignments.md).

## Process shape

Every service is one process, SQLite or memory, no extra broker. Background work
is in-process. That is a load-bearing limit: a second process would need a second
way to know who the request is for, and that is how identity leaks into a queue
payload.

## Size

A module past a thousand lines is a module nobody can hold. The family checker
fails it. [ADR-0005](adr/0005-no-file-over-1000-lines.md).
