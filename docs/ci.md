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

Install `FAMILY_GITHUB_TOKEN` as an Actions repository secret in every repository:
a fine-grained PAT with Contents read-only on exactly the nine repositories. Callers
pass it with `secrets: inherit`. Every uv fetch job configures git before fetching;
the privileged test container installs git first. Docker receives it as a BuildKit
secret; parity's meta checkout uses it with `persist-credentials: false`.

The reusable workflow declares the secret required. Inherited secrets can still be empty
on a public fork: authentication then skips configuration and public dependencies fetch
anonymously. Private sources need the secret. Fork pull requests do not normally receive
repository secrets. See [private-repos.md](private-repos.md) for setup and rotation.

When the meta repository is private, allow Actions access from repositories under the
same owner. Public callers cannot use a private reusable workflow. For a copy, run
`scripts/retarget.py OWNER` and publish its `v1` tag; `--ref` chooses a different ref.
