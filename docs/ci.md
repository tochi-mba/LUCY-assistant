# Calling the family CI workflow

Each service repository keeps a thin workflow that calls
[`service.yml`](../.github/workflows/service.yml) in this repository. The reusable
workflow owns lockfile, format, lint, types, imports, the Python matrix, generated-file
drift, the Docker probe, `pip-audit` (continue-on-error), and family parity
(continue-on-error).

Action majors used here: `actions/checkout@v4`, `astral-sh/setup-uv@v6`,
`actions/upload-artifact@v4`. Bump them in `service.yml`, not in every caller.

## Default caller (ten lines)

```yaml
name: CI
on:
  push:
    branches: ["**"]
  pull_request:
jobs:
  service:
    uses: tochi-mba/LUCY-assistant/.github/workflows/service.yml@main
```

That is Keyring-api, User-api, Persona-api, Settings-api, and Spotify-api once they
have switched. Settings-api also regenerates committed files:

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

Both now have a `dev` dependency group, a `make imports` target and both probes, so
the caller above is all either one needs. Web-search-api's Dockerfile healthcheck still
calls `/health`; `/healthy` is served as an alias, and the two can be reconciled whenever
that image is next touched.

Spotify-api's image needs `SPOTIFY_API_KEYRING_*` to boot. Until the Dockerfile can
start with baked-in dummies, set `run-docker: false` and keep the service's own
probe, or pass the dummy environment in that service's wrapper job.
