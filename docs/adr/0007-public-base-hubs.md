# ADR-0007: public family repositories by default

**Status:** accepted

## Context

ADR-0006 allowed every repository in the family to be private. That protected
product surfaces, but it also blocked anonymous clones of the meta-repo from
fetching shared clients and the rest of the system. Developers hitting a private
wall before they could run or learn from the family was the wrong default.

## Decision

On the canonical owner, the published family repositories are **public by default**:

| Repository | Role |
| --- | --- |
| `LUCY-assistant` (meta) | Reusable CI, broker action, bootstrap, docs, example API |
| `Keyring-api` | Identity issuer and `keyring_client` |
| `Settings-api` | Per-person knobs and `settings_client` |
| `User-api` | Structured facts about the person |
| `Persona-api` | The assistant's model of itself |
| `Web-search-api` | Search / scrape / summarise |
| `Spotify-api` | Track lookup and playback |
| `Environments-api` | Sandboxed shells |

A fork or copy under another owner may still make any repository private. The table
above is the upstream default so `repos.txt` clones and tagged client fetches work
for anyone who clones this meta-repo. Entries in `repos.txt` that the signed-in
account cannot see are skipped.

## Consequences

- Bootstrap clones every public service in `repos.txt` without a GitHub login.
- `uv sync` can fetch `keyring_client` / `settings_client` anonymously.
- CI continues to use the shared public app and OIDC broker for private callers
  and private copies.
- [examples/hello-api](../../examples/hello-api) shows how to add a new API.

## What would change our minds

Publishing the clients to a package index would remove the need for public git
hubs for dependency fetch.
