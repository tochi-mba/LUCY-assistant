# hello-api (example)

A minimal LUCY-family FastAPI service. Copy this folder when you add your own API.

It is a **JWKS-only** consumer: it verifies keyring tokens locally and never calls
keyring's `/v1/internal`. That is the same pattern as User-api, Persona-api, and
Settings-api. Credential consumers (Spotify-api, Web-search-api, …) add a service token
and `keyring_client.CredentialClient` on top of this shape.

This example lives under the meta-repo so clones can read it without a separate
repository. It is **not** one of the eight family services and is not in `repos.txt`.

## What it shows

| Piece | Where |
| --- | --- |
| Family Makefile verbs + `make check` | `Makefile` |
| OIDC CI caller (no Actions secrets) | `.github/workflows/ci.yml` |
| Config with `env_prefix`, `extra="forbid"`, unknown-env refusal | `src/hello_api/core/config.py` |
| `/healthy` (liveness) and `/ready` (readiness) | `src/hello_api/api/routers/health.py` |
| Bearer JWT verification with `ExactAudience` | `src/hello_api/auth/verifier.py` |
| One authenticated route | `GET /v1/whoami` |
| BuildKit `github_token` secret on `uv sync` | `Dockerfile` |
| Shared client from the public Keyring-api tag | `[tool.uv.sources]` in `pyproject.toml` |

## Run locally

Needs a running Keyring-api (port 8001) only if you want real tokens. The test suite
uses `keyring_client.testing` and does not need a live keyring.

```bash
cd examples/hello-api
cp .env.example .env
uv sync --group dev
make check
make run   # http://127.0.0.1:8090/docs
```

Mint a token against keyring with audience `hello`, then:

```bash
curl -sH "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/v1/whoami
```

## Graduate into a real family service

1. Copy this folder beside the meta-repo as `Your-api/` (or create a new GitHub
   repository and push the copy).
2. Rename the package (`hello_api` → `your_api`), env prefix (`HELLO_` → `YOUR_API_`),
   audience (`hello` → your audience), and port (pick the next free family port or keep
   a private one until you join compose).
3. Append a line to the meta-repo `repos.txt`, re-run bootstrap, and add the repository
   to the family GitHub App installation.
4. From the meta-repo: `python scripts/parity.py --repo Your-api`.
5. Follow [docs/adding-a-service.md](../../docs/adding-a-service.md) for compose,
   settings grants, and credential-consumer extras.

`python scripts/parity.py` does not score this example in place; parity runs against
sibling checkouts listed in `repos.txt`.
