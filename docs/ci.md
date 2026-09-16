# Calling the family CI workflow

Each service repository keeps a thin workflow that calls
[`service.yml`](../.github/workflows/service.yml) in this repository. The reusable
workflow owns lockfile, format, lint, types, imports, the Python matrix, generated-file
drift, the Docker probe, `pip-audit` (continue-on-error), and family parity
(continue-on-error).

Action majors used here: `actions/checkout@v4`, `astral-sh/setup-uv@v6`,
`actions/upload-artifact@v4`. Bump them in `service.yml`, not in every caller.

## Default caller

```yaml
name: CI
on:
  push:
    branches: ["**"]
  pull_request:
permissions:
  contents: read
  id-token: write
jobs:
  service:
    uses: tochi-mba/LUCY-assistant/.github/workflows/service.yml@v1
```

The callers use the moving, tested `v1` workflow tag. Only advance it after the
meta workflow changes pass their checks; client dependency tags remain immutable. Settings-api also regenerates committed files:

```yaml
    with:
      generated-files-command: |
        uv run python scripts/dump_schema.py
        uv run python scripts/gen_catalogue.py
```

## Environments-api

The suite exercises real sandbox tiers and needs root plus `unshare`.

```yaml
    with:
      privileged-container: true
```

It serves `/healthy` and `/ready` like every sibling, with `/health` and `/health/ready`
kept as aliases, so the default healthcheck path needs no override.

## Web-search-api and other Chromium jobs

Services that drive Chromium turn the live-browser job on:

```yaml
    with:
      live-browser-job: true
      extras: "--all-extras --group dev"
```

Web-search-api sets `live-browser-marker: browser`. Callers that use the default
marker keep `live_browser`. Both need a `dev` group, `make imports`, and both
health probes.

Spotify-api passes dummy `SPOTIFY_API_KEYRING_*` boot configuration with `docker-env`.

## Private repositories

CI authenticates as the public **lucy-assistant family CI** GitHub App, installed on the
repositories it may read with *Contents: read-only*. The caller's `id-token: write`
permission lets GitHub issue an OIDC identity for the job; it does not grant repository
write access. The canonical family-token action sends that identity to the Cloudflare
broker. The broker verifies the trusted `service.yml@v1` workflow and repository owner,
then mints a one-hour token scoped to that owner's app installation.

No app key or long-lived token is stored in a user's repository. Every job configures git
with the short-lived result; the Docker job passes it as a BuildKit secret; the parity
checkout does not persist it. A laptop uses `gh auth login`, not the broker.

The canonical meta repository and `v1` workflow stay public so a private repository under
any owner can call them. `scripts/retarget.py OWNER` therefore keeps the caller on
`tochi-mba/LUCY-assistant@v1` by default; `--self-host-ci` is for an operator with a
separate app and broker. [private-repos.md](private-repos.md) covers installation.
