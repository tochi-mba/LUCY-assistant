# ADR-0004: family port assignments

**Status:** accepted

## Context

Several services default to 8000. Persona-api and User-api both default to 8002.
They cannot run on one host without fighting, and "whatever is free" is not a
sentence you want in a runbook.

## Decision

| Service | Port |
| --- | ---: |
| Keyring-api | 8001 |
| User-api | 8002 |
| Settings-api | 8003 |
| Persona-api | 8004 |
| *(reserved)* | 8005 |
| Web-search-api | 8006 |
| Spotify-api | 8007 |
| Environments-api | 8008 |

Compose publishes those ports on the host, and each image listens on the same
number inside the container. The family number is the one a human types.

Issuer URLs stay `http://127.0.0.1:8001` so a token minted against a published
port verifies against the same `iss` a local `make run` would use. Inside the
compose network, JWKS and credential fetches use `http://keyring:8001`.

## Why

**8001 is already keyring**, in every sibling `.env.example`. Starting the
sequence there, rather than at 8000, means keyring does not move.

**Gaps are worse than a table.** A service that "just uses 8000" looks fine in
isolation and collides the moment compose starts.

## What it costs

Applied 2026-09-16: every repository's default now matches the table, in its
config, its Dockerfile, its `make run`, its `.env.example` and its
documentation. Four services moved, so anybody with a bookmark, a script or a
reverse proxy pointing at the old number has to change it once — Persona-api
from 8002, Web-search-api and Spotify-api from 8000, and
Environments-api from 8080. Each move is recorded in that repository's
changelog as a breaking change.

## What would change our minds

A reverse proxy that binds :443 and routes by host name would make the numbers
local-only. They would still need to be unique on a laptop. We would keep the
table.
