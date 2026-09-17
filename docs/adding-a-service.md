# Adding a service to the family

Copy [examples/hello-api](../examples/hello-api) when you need a new HTTP service.
That tree is a complete JWKS-only FastAPI service: family Makefile verbs, the OIDC
CI caller, `/healthy` and `/ready`, config that refuses unknown `HELLO_*` variables,
and one authenticated route. It is not itself a family member and is not listed in
`repos.txt`.

## 1. Copy and rename

```bash
cp -R examples/hello-api ../Your-api
cd ../Your-api
```

Rename the package (`hello_api` → `your_api`), the env prefix (`HELLO_` → `YOUR_API_`),
the audience (`hello` → your service audience), the port, and the Docker/Make image
names. Keep `/healthy` and `/ready` spelled exactly that way.

## 2. Choose the auth pattern

| Pattern | When | What to add |
| --- | --- | --- |
| **JWKS-only** (hello-api, User-api, Persona-api, Settings-api) | You only need to know *who* is calling | `keyring_client` verifier; no `KEYRING_SERVICE_TOKENS` entry |
| **Credential consumer** (Spotify-api, Web-search-api, Environments-api, …) | You fetch third-party secrets from keyring | Service token + `CredentialClient`; add the service to `scripts/genenv.py` `KEYRING_CONSUMERS` |
| **Settings reader** | Per-person knobs | `settings-client` and a grant in Settings-api |

Shared clients come from the **public** hubs:

```toml
[tool.uv.sources]
keyring-client = { git = "https://github.com/tochi-mba/Keyring-api", subdirectory = "clients/python", tag = "keyring-client-v0.1.0" }
settings-client = { git = "https://github.com/tochi-mba/Settings-api", subdirectory = "clients/python", tag = "settings-client-v0.1.0" }
```

Clones can fetch those tags anonymously from the public Keyring-api and Settings-api
hubs.

## 3. Join the family desk

1. Create the GitHub repository (public or private) and push the copy.
2. Append one line to this meta-repo's `repos.txt` (public) or `.repos.local.txt`
   (gitignored; copy from `.repos.local.txt.example`): `Your-api https://github.com/<owner>/Your-api.git`.
3. Re-run `bash scripts/bootstrap.sh` (or the PowerShell bootstrap) so the checkout appears beside this file.
4. Add the repository to the [lucy-assistant family CI](https://github.com/apps/lucy-assistant-family-ci) installation (Settings → Applications → Installed GitHub Apps → Repository access).
5. From this meta-repo: `python scripts/parity.py --repo Your-api`.

Optional: wire the service into `docker-compose.yml`, `scripts/genenv.py`, and
`LUCY-assistant.code-workspace` when it should run with the rest of the family.
Family ports are 8001–8008; see [ADR-0004](adr/0004-port-assignments.md).

## 4. CI caller

Every service uses the same thin caller. No Actions secrets:

```yaml
name: CI
on:
  push:
    branches: ["**"]
  pull_request:
  workflow_dispatch:
permissions:
  contents: read
  id-token: write
jobs:
  service:
    uses: tochi-mba/LUCY-assistant/.github/workflows/service.yml@v1
```

## Checklist

- [ ] `make check` is green on 3.12 (and `make matrix` before a push).
- [ ] `python scripts/parity.py --repo Your-api` is green.
- [ ] Audience is documented; keyring mints for it with `POST /v1/auth/service-token`.
- [ ] No GitHub token in `.env`, Docker `ARG`/`ENV`, or committed files.
- [ ] Dockerfile keeps `# syntax=docker/dockerfile:1` and a `github_token` secret mount on every `uv sync` RUN.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the family standard and
[private-repos.md](private-repos.md) for sign-in and the shared CI app.
