# API

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/healthy` | none | Liveness |
| GET | `/ready` | none | Readiness (keyring JWKS) |
| GET | `/v1/whoami` | Bearer JWT, audience `hello` | Echo verified `sub` |

OpenAPI lives at `/docs` when the process is running.
