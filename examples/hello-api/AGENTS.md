# AGENTS.md

Working notes for the hello-api example. Read this before editing.

## What this service is

**hello** is a teaching FastAPI service: liveness, readiness, and one authenticated
route that echoes the verified account id. It verifies keyring JWTs locally and does
not hold personal data or third-party credentials.

## Commands

| Command | What it does |
| --- | --- |
| `make install` | Create the venv and install everything. |
| `make check` | Lint, types, import contracts, tests at 100% branch coverage. |
| `make run` | Serve on :8090 with reload. |
| `make test` | Tests only. |

## Invariants

- `/healthy` does no I/O and never fails.
- `/ready` reports keyring JWKS reachability and answers 503 when unusable.
- Every `/v1` route takes identity only from a verified Bearer token.
- No `pragma: no cover`. No setting that disables verification.
