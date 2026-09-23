# Baseline: the first composed run

The family had never been brought up together. `docs/implementation-status.md` said so
plainly -- *"Nothing has been validated under `make up` with the whole family running, and
no real model has been called."* This document records the first run that worked, what it
took to get there, and which numbers are still missing.

It is deliberately narrow: it records what was **observed on this machine on 2026-09-23**,
not what the code is believed to do.

## What now works

`make up` brings up nine services and all nine report healthy to docker.

| Port | Service | `/ready` | `status` |
| ---: | --- | ---: | --- |
| 8000 | lucy | 503 | `degraded` -- no model credential (see below) |
| 8001 | keyring | 200 | `ok` |
| 8002 | user | 200 | `ok` |
| 8003 | settings | 200 | `ready: true`, 134 settings |
| 8004 | persona | 200 | `ok` |
| 8006 | web-search | 200 | `ok` |
| 8007 | spotify | 200 | `ready` |
| 8008 | environments | 200 | `ready`, `sandbox_tier: namespace` |
| 8009 | memory | 200 | `ok` |

Services outside the default compose profile were not exercised.

## The hub's HTTP surface answers correctly

Checked against the running container, with no credential presented:

| Request | Result |
| --- | --- |
| `GET /healthy` | `200` |
| `GET /v1/capabilities` | `401` |
| `POST /v1/sessions` | `401` |
| `GET /.well-known/oauth-protected-resource` | `200` (RFC 9728) |
| `GET /mcp` | `405` -- the MCP surface is POST-only, as specified |

The refusal is the documented shape rather than a bare status:

```json
{"type": "https://lucy-api.invalid/problems/unauthorized",
 "title": "Unauthorized", "status": 401,
 "detail": "a keyring token is required",
 "request_id": "5b1c4d7501c4497cba62b7b0389232df"}
```

RFC 9457 `application/problem+json`, a `request_id` in the body, and a `detail` that says
what is needed without echoing what was sent. Auth is enforced before routing, and the
well-known document is served without one -- which is what a client needs in order to know
where to get a token.

None of this had been observed outside the test suite before today.

## Four defects found by running it

Every one of these was invisible to `make check`, which is green and has been green
throughout. Three are fixed here; the fourth is recorded.

### 1. `.env.family` predated Lucy and Memory entirely

The generated environment file on this machine held nine variables. The current
`scripts/genenv.py` produces twelve. Missing: `LUCY_KEYRING_SERVICE_TOKEN`,
`LUCY_SETTINGS_API_TOKEN`, `LUCY_MEMORY_API_TOKEN`, `MEMORY_SERVICE_TOKENS`,
`KEYRING_EXCHANGE_AUDIENCES`. Present but stale: two variables for a service that is no
longer in the published list.

So **Lucy had never had credentials to call any sibling**, and would have failed every
settings and memory call with a 401 that looks exactly like a correctly configured service.
That is the failure mode the plan warned about in the abstract; here it was the actual
state of the box.

Regenerating is the fix, but `genenv.py --force` rotates `KEYRING_MASTER_KEY`, which
encrypts the credentials already in `keyring.db` (202 KB of real data on this machine).
The key was preserved and every other secret rotated -- they are shared bearer strings
whose only readers are containers started from the same file.

**Worth fixing properly:** there is no way to refresh the generated file without rotating
the one secret that must not rotate. A `--preserve KEYRING_MASTER_KEY` flag, or splitting
the master key into its own generated file, would make this a one-command operation instead
of a careful one.

### 2. settings-api refused to start on a namespace that no longer exists

```
ConfigurationError: invalid configuration: services.<name>.namespaces:
  namespaces names '<namespace>', which is not a namespace; this build has
  common, environments, keyring, lucy, memory, persona, search, spotify, user
```

A consequence of (1): the stale file carried a grant naming a namespace this build of
settings-api does not define. settings-api validates every grant at startup and refuses
to boot on one it cannot honour, which is the right call -- a grant that silently does
nothing is worse -- but it means one stale row takes the whole family down. Fixed by
regenerating.

### 3. memory-api was sent a variable it refuses to read

```
RuntimeError: unknown environment variables: MEMORY_KEYRING_SERVICE_TOKEN
```

`genenv.py` listed memory-api in `KEYRING_CONSUMERS`, so it wrote that variable. Memory-api
verifies keyring's JWTs but never calls keyring's `/v1/internal`, so it declares no
`keyring_service_token` field -- and its config refuses unknown `MEMORY_*` variables rather
than ignoring them, which turns a harmless extra into a crash loop.

Fixed by removing the row. `memory-api` remains in `LUCY_EXCHANGE_AUDIENCES`: an audience
Lucy may mint for is a different thing from a consumer that calls keyring, and conflating
them is what produced the bad row.

`tests/test_genenv.py` iterates the rows generically, so it passed before and after. **The
gap is that nothing cross-checks a generated variable name against the settings class meant
to read it** -- which is a parity check worth adding, since every service in the family
declares its configuration the same way.

### 4. memory-api's data volume arrived read-only to its own user

`/ready` answered 503 with `database: {reachable: false, reason: "OperationalError"}` while
docker reported the container healthy, because the healthcheck asks `/healthy` and the
process was alive -- it simply had nowhere to write.

The runtime stage was still the `examples/hello-api` skeleton: a user named `hello`, and no
`/var/lib/memory` in the image. Docker seeds a named volume from the image path, so with no
directory to copy from, the volume arrived `root:root drwxr-xr-x` and uid 10001 could not
create `memory.db`. Keyring-api has always done this correctly; memory-api now matches it.

**Worth noting for the family:** a container can be healthy and unusable at the same time.
`depends_on: service_healthy` gates on `/healthy`, which is liveness. Anything that actually
needs a working dependency has to gate on `/ready`.

## Readiness is not a uniform signal

Four different shapes across nine services:

```
lucy, keyring, user, persona, web-search, memory
    {"status": "ok"|"degraded", "checks": {"<name>": {"status", "detail"}}}
settings
    {"ready": true, "settings_count": 134, "checks": [{"name", "ready", "detail"}]}
spotify
    {"status": "ready", "dependencies": {"spotify": "ok"}}
environments
    {"status": "ready", "sandbox_tier": "namespace", "keyring": {"status", "error"}}
```

`status` versus `ready`; an object of checks versus an array; three different vocabularies
for success (`ok`, `ready`, `true`). A hub cannot gate on this generically, and today Lucy
hand-writes a probe per service. This is direct evidence for the proposed parity check that
every service expose a readiness signal a hub can consume.

## What the sandbox says

`environments` reports `sandbox_tier: "namespace"` with `min_sandbox_tier: "directory"` and
`allow_network: true`. Namespace isolation is the real tier, so the Linux sandbox works
under compose. `implementation-status.md` lists this as an open item on M4 -- *"Linux
sandbox runtime validation remains because Environments-api cannot import `fcntl` on this
Windows host"* -- and it can now be closed for the composed path. Running the hub natively
on Windows is still affected.

## What is still missing

**No model has been called.** `LUCY_MODEL_KEYS` is not set on this machine, and Lucy's
`/ready` says so correctly:

```json
{"status": "degraded",
 "checks": {"keyring": {"status": "ok"},
            "database": {"status": "ok"},
            "model": {"status": "degraded", "detail": {"configured": false}}}}
```

That is the designed first-run behaviour, not a defect: keyring and the database are
reachable, and the only missing piece is a credential a person supplies. Supply one with
`lucy models connect <provider>` and the 503 becomes a 200.

Everything below therefore remains **unmeasured**, and every one of these is a gate for the
typed-decision work, which is why they are listed rather than estimated:

| Metric | Why it is needed |
| --- | --- |
| Stable vs volatile prompt tokens per turn | The central hypothesis of the decision layer is that it shrinks the volatile half. Unfalsifiable until measured. |
| Cache-read ratio | A later gate is "tokens down with the cache-read ratio flat". There is no flat yet. |
| Tool-result share of context | Quoted as 96.3% from someone else's baseline. Ours is unknown. |
| Turn wall-clock p50/p90 | A 1 s decision budget is either 3% or 30% of a turn. |
| Model cost per turn | The 2% decision-cost target has no denominator. |
| Bound-capability count and deferral rate | `DEFER_ABOVE = 6` with eight packs: it is not known whether deferral fires at all. |

## Reproducing this

```bash
python scripts/genenv.py --force      # see the KEYRING_MASTER_KEY caveat above
make images && make up
for p in 8000 8001 8002 8003 8004 8006 8007 8008 8009; do
  printf '%s ' "$p"; curl -s --max-time 8 "localhost:$p/ready" | head -c 120; echo
done
```

`make check` remains green throughout and caught none of the four defects above. That is
the finding behind the finding: the gates verify the hub against itself, and nothing
verified it against the family until it was run.
