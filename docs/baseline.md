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

## What was still missing, at the time of the run above

**No model had been called.** `LUCY_MODEL_KEYS` is not set on this machine, and Lucy's
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

Those numbers were unmeasured when this section was written. They are measured now; see
**The first real conversations** below.

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

## The first real conversations

**Recorded 2026-09-23, on the same machine, later the same day.** The section above ends by
saying no model had been called. One has now, through
[clyde](https://github.com/tochi-mba/clyde) -- a harness that serves
`POST /v1/chat/completions` from the Claude Code CLI under a desktop subscription, so a turn
needs no API key. Lucy reaches it with one environment variable:

```bash
LUCY_MODEL_BASE_URLS='{"lmstudio":"http://host.docker.internal:8127/v1"}'
```

Ten conversations were held, chosen so each answers a different question rather than proving
the same thing ten times. All ten now pass. Six defects in this repo had to be fixed first,
and every one of them is listed below, because the pattern matters more than any single fix:
**2,522 tests passed throughout**, and none of the six was visible to any of them.

### What was measured

Three components of one trivial turn, measured by sending the same request to clyde three
times and diffing `prompt_tokens`:

| Component | Tokens | Share |
| --- | ---: | ---: |
| Claude Code's own prompt, with every tool disallowed | 2,905 | 15% |
| Lucy's system prompt | 4,462 | 24% |
| **Lucy's plan schema** | **11,495** | **61%** |
| One trivial turn, total | 18,862 | |

The third row is the finding. The plan schema is the largest single thing Lucy sends, by a
wide margin -- and `GET /v1/sessions/{id}/context` reports `"tools": 0`. It is not counted
because it travels as `response_format`, not as a message, so the hub's own accounting sees
roughly a third of what it actually sends:

| | Hub reports | Provider charged |
| --- | ---: | ---: |
| Shortest turn ("say the single word: ready") | 5,940 | 19,491 |

That gap is not a rounding error and it is not clyde's floor. It is the schema.

### Per-turn numbers

One session per prompt, so history does not confound the bands. Tokens are what the provider
reported; bands are what the hub reports.

| Turn | s | Rounds | Input | Output | Cache read | System | Pinned | History |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| "what can you do right now?" | 34.4 | 2 | 26,351 | 594 | 13,694 | 5,688 | 240 | 905 |
| "say the single word: ready" | 12.1 | 1 | 12,644 | 4 | 6,847 | 5,688 | 240 | 12 |
| "what do you know about me?" | 24.3 | 2 | 25,633 | 228 | 13,694 | 5,688 | 239 | 257 |
| a request to be declined | 16.2 | 1 | 12,662 | 121 | 6,847 | 5,688 | 244 | 112 |
| 37,072 characters of meeting notes | 16.3 | 1 | -- | -- | -- | 5,688 | 251 | 9,335 |

### The six metrics that were blocking

| Metric | Measured |
| --- | --- |
| Stable vs volatile prompt tokens | Stable is 18,862 and volatile is the history band: 12 tokens on the shortest turn, 9,335 on a 37 KB input. The stable half dominates by one to three orders of magnitude, and 61% of it is the schema. |
| Cache-read ratio | **34–35%** of prompt tokens, consistently: 6,847 / 19,491 on a one-round turn, 13,694 / 40,045 on a two-round turn. Stable enough to be a usable gate. |
| Tool-result share of context | Not 96.3%. On these turns the whole history band -- messages *and* tool results -- is 0.2% to 13% of what the hub counts, and under 2% of what is actually sent. The 96.3% figure came from somebody else's system and should not be carried forward. |
| Turn wall-clock | p50 ≈ 16 s, range 12.1–34.4 s. Rounds, not input size, drive it: a 37 KB input took 16.3 s in one round, while a two-round turn on a nine-word question took 34.4 s. A 1 s decision budget is therefore ~6% of a median turn. |
| Cost per turn | Now recorded per turn in `input_tokens`, `output_tokens` and `cache_read_tokens` -- the columns existed from the first migration and nothing ever wrote them, so `GET /usage` answered zero for every session ever. Cash cost stays unmeasured on a subscription, where the provider's own figure is notional. |
| Bound capabilities and deferral rate | Deferral fires. With eight packs and `DEFER_ABOVE = 6`, a workspace request produced `capabilities.use` returning *"workspace will be available next turn"*, then the write on the round after. Two rounds is the floor for any turn touching a deferred pack. |

### What the conversations showed

| # | Asked | Result |
| --- | --- | --- |
| 1 | "what can you do right now?" | Answers in prose about itself, separating connected from not, and names what timed out |
| 2 | "of those, which would you set up first?" | Resolves the reference against the previous turn and hedges on what it cannot know |
| 3 | "remember I prefer tea over coffee" | Plan, approval gate, `notes.setFact`, confirmation |
| 4 | "what do you know about me?" | Reads the fact back **in a new session**, and flags it as unconfirmed rather than asserting it |
| 5 | write a file, then read it back | `capabilities.use`, approval, `workspace.write`, `workspace.read`; reported its own failed first attempt |
| 6 | "play me some jazz" | Declines, says what connecting would allow, gives a next step |
| 7 | 37 KB of meeting notes | Found the three themes, and caught a bug in the generated fixture the author had missed |
| 8 | a privilege-escalation request | Declines with a reason, and notes it is sandboxed regardless |
| 9 | hang up mid-turn, reconnect | Turn completed with nobody listening; `starting_after` replayed all nine missed events, each exactly once |
| 10 | two clients, one session | Second input queued behind the first; both answered, in order |

Scenario 9's cursor is a `sequence_number`, not an `event_id`. Passing an `event_id` returns
an empty stream rather than an error, which is worth knowing before debugging a client.

### The six defects, and why no test saw them

| Defect | Why the suite could not see it |
| --- | --- |
| The provider was asked for a model named `lmstudio:sonnet` -- the session's whole spec rather than the model id | `ScriptedProvider` serves a pre-built reply and never reads `Request.model` |
| A plan wrapped in a ```json fence parsed as nothing, so the steps never ran, the turn was recorded a **success**, and the person was shown wire format | No test sends a fenced reply, because nothing scripted ever fences |
| The model registry got `http_timeout_seconds` (10 s), the figure for a sibling that is up or down. `wire.DEFAULT_TIMEOUT` had said 120 s since it was written | Nothing in the suite makes a call that takes longer than instant |
| A failed turn recorded no reason anywhere -- API, events or log. `error_during_execution` was the whole report | Tests assert on `Termination`, which is present; the sentence explaining it is what was missing |
| **An approved write never ran.** The whole plan is gated, so nothing executes; the plan is held in memory and dropped; resuming re-asks the model, which sees only its own request and `{"approved": true}` in the person's voice and concludes the write happened | `test_a_gated_write_parks_until_the_person_answers_then_resumes` scripted plain speech as the reply after approval and asserted only that the turn completed |
| Per-turn token counts were computed every round and never written down | `GET /usage` was never asserted against a turn that had actually spent anything |

The fifth is the one that matters. An approval gate that authorises a write which then does
not happen is the worst failure this system can have: the person is told they approved it, the
model is told the read failed, and nothing anywhere says the write was dropped. It reproduced
on two separate sessions before it was understood.

### What is still unexercised

* `Stop.refusal` never fired. Scenario 8 was declined by the model in prose, which is a
  different path from a provider-level refusal, and clyde has no way to produce one.
* Cash cost per turn, for the reason given above.
* The typed-decision layer, which remains off. These conversations are the start of the
  golden set it needs, not a test of it.
