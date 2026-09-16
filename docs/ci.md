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
jobs:
  service:
    uses: tochi-mba/LUCY-assistant/.github/workflows/service.yml@v1
    secrets: inherit
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

## Media-tool and Web-search-api

Both drive Chromium, so turn the live-browser job on:

```yaml
    with:
      live-browser-job: true
      extras: "--all-extras --group dev"
```

Web-search-api also sets `live-browser-marker: browser`; Media-tool uses the default
`live_browser`. Both have a `dev` group, `make imports`, and both health probes.

Spotify-api passes dummy `SPOTIFY_API_KEYRING_*` boot configuration with `docker-env`.

## Private repositories

CI authenticates as the family's GitHub App, installed on the nine repositories with
*Contents: read-only*. `uv run scripts/connect_github.py` creates and installs it from
the browser and stores its client id and private key as the secrets
`FAMILY_APP_CLIENT_ID` and `FAMILY_APP_PRIVATE_KEY` on every family repository. The
reusable workflow declares both as optional secrets and callers pass them with
`secrets: inherit`. Every job that runs `uv sync` first mints a one-hour token with
`actions/create-github-app-token` and configures git with it (the privileged test
container installs git before that); the Docker job passes the token as a BuildKit
secret; the parity job checks out this repository with it and `persist-credentials: false`.

Inherited secrets can still be empty, on a fork without the app or a pull request from a
fork, and the workflow then fetches anonymously, which works while the sources are public.
Private sources need the app. [private-repos.md](private-repos.md) covers connecting and
rotating it.

When this repository is private, allow its workflows to be used by repositories under the
same owner (Settings → Actions → General → Access). A public repository cannot call a
private reusable workflow. For a copy under another owner, run `scripts/retarget.py OWNER`
and publish that copy's `v1` tag; `--ref` chooses a different ref.
