# Running the hub

## Host and port

The hub listens on **8000** — below the family block, because it is the front door and the
other eight are what it calls. See [ADR-0004](adr/0004-port-assignments.md) and
[ADR-0010](adr/0010-ports-8000-and-8009.md).

```bash
make install
make run                 # http://127.0.0.1:8000/docs
```

## Configuration

Every knob is an environment variable prefixed `LUCY_`, and **an unknown one under that
prefix is a startup error**. That matters more here than in a leaf service: the hub holds
the base URL of every sibling, and a misspelled `LUCY_KEYRNIG_BASE_URL` would otherwise
start a process that looks healthy until the first token needs verifying.

| Variable | Default | What it is |
| --- | --- | --- |
| `LUCY_HOST` / `LUCY_PORT` | `127.0.0.1` / `8000` | Where to listen. The default expects a TLS-terminating proxy in front. |
| `LUCY_ENVIRONMENT` | `local` | Reported by both health routes. |
| `LUCY_LOG_LEVEL` / `LUCY_LOG_FORMAT` | `INFO` / `json` | `console` is for a terminal; `json` is for anything that collects logs. |
| `LUCY_AUDIENCE` | `lucy-api` | The `aud` this hub accepts, exactly. Must equal its name in keyring's `KEYRING_SERVICE_TOKENS` **and** its `audience_prefix` in settings-api's `SETTINGS_API_SERVICES`. |
| `LUCY_KEYRING_BASE_URL` | `http://127.0.0.1:8001` | Keyring. |
| `LUCY_KEYRING_JWKS_URL` | `.../.well-known/jwks.json` | Where the verifying keys come from. |
| `LUCY_KEYRING_ISSUER` | `http://127.0.0.1:8001` | Pinned `iss`. Must match what keyring mints. |
| `LUCY_KEYRING_SERVICE_TOKEN` | *(empty)* | This hub's entry in `KEYRING_SERVICE_TOKENS`, for two-credential calls. |
| `LUCY_USER_API_BASE_URL` … `LUCY_MEMORY_API_BASE_URL` | the family ports | One base URL per sibling. |
| `LUCY_SETTINGS_API_TOKEN` | *(empty)* | The hub's service token for settings-api. |
| `LUCY_JWKS_CACHE_SECONDS` | `3600` | How long verifying keys are cached. |
| `LUCY_JWKS_MIN_REFETCH_SECONDS` | `30` | Anti-DoS: without it, a stream of tokens with random `kid` headers is one outbound fetch per inbound request. |
| `LUCY_HTTP_TIMEOUT_SECONDS` | `10` | Outbound timeout. |

A fact about a **person** — which model they prefer, how much context goes to memory,
whether a destructive tool may run without asking — is not configuration. It belongs in
settings-api under the `lucy` namespace.

## Health

`GET /healthy` does no I/O and never fails: it says the process is running, nothing more.
An orchestrator restarts a container whose liveness check fails, and restarting a process
does not fix the service it depends on.

`GET /ready` reports each dependency and answers 503 when one is unusable. Neither route is
authenticated, and neither reports a name, an account, or a count that moves when one
person acts — a counter that moves when one person acts is an oracle.

## In compose

`make up` starts the hub with the rest of the family on 8000–8009. `scripts/genenv.py`
writes `.env.family`, which holds the service tokens; it never contains a GitHub credential
and never prints a value.
