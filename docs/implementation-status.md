# Lucy implementation status

The complete requested scope is [the supplied plan](lucy-plan.md). An increment passing
tests does not mean the full plan is complete. This ledger records verified completion and
remaining work, and must survive interruptions.

It is deliberately pessimistic. Anything not verified by a run of `make check` on this
machine is listed as in progress, however finished it looks.

## Verified complete

- **The family runs on Python 3.12**, hub included, with ADR-0008 recording why 3.13 is
  declared supported but not gated in CI.
- **The hub is a conforming family service.** `python scripts/parity.py` scores all nine
  repositories, this one among them.
- **The `lucy` command.** Global install through `uv tool install`, status, version, serve,
  setup, connect, doctor and config. Documented in [docs/cli.md](cli.md). Verified working
  from a directory outside the checkout, against a live hub.
- **The context engine.** Five zones ordered by volatility, five independently budgeted
  bands, the live state block, framing, the injection scrubber, the prompt sections, the
  memory topic index, and compaction as a projection over an append-only transcript.
  The live runner and `GET /v1/sessions/{id}/context` share this assembly path. Only authored
  instructions use the provider system channel; claims, transcript items, tool results and
  live state remain data messages. The reclamation ladder drops old tool results (never
  `notes.*`) and thinking before auto-compacting once per live turn, using
  `max_context_tokens`, `reserve_percent`, `warn_at_percent`,
  `compaction_trigger_percent`, `history_turns_kept` and `tool_results_kept`.
  `interrupt` stops the live turn and keeps its progress; `rollback` hides that turn's
  items from the next prompt. Cancel is cooperative: `cancel_requested` is polled before
  and after each model round. Documented in [docs/context.md](context.md),
  [docs/sessions.md](sessions.md), [docs/prompts.md](prompts.md) and
  [docs/memory.md](memory.md).
- **Settings catalogue, grouped by capability.** Thinking, execution limits, prompt-feed
  masters, and a toggle per feed field are in the `lucy` namespace; siblings own their
  playback, shell and search knobs. `max_llm_turns` is enforced by the main loop and
  `max_subagent_turns` is the child-loop budget. `TurnPolicy` is the only place a turn
  reads those numbers: helper concurrency, memory-write policy, omitted new-session
  defaults (`model`, thinking, `permission_mode`, `input_policy`, `incognito`),
  `stream_thinking`, `log_message_content`, and the refuse keys
  that 503 a turn when settings cannot be reached. Account-wide versus
  profile-wide scopes are exclusive. Settings-api passed its full `make check` on this
  machine.
- **M1 durable sessions and the live turn runner.** Sessions, append-only items, idempotent
  `POST /inputs`, durable queued turns, the in-process single-writer supervisor,
  provider-backed model/tool rounds, real context projection, and resumable native SSE are
  wired. Queued user inputs retain conversational order even when assistant output is
  appended later. A process restart fails turns left `running` (they are not replayed,
  because a tool that already ran must not run again) and then drains what was still
  queued. Turns parked on a person (`input_required`, `auth_required`) survive the restart.
- **Initial live prompt feeds.** Persona identity, pinned persona data, pinned account
  fields, current playback, active playback device, and safe attached-workspace state are
  fetched once per turn with entry-level provenance and per-field settings. Account pins
  are a standing feed of their own, never mixed into memory retrieval. Execution authority
  for a queued turn is held only in memory and is never persisted.
- **Live memory index.** Each turn fetches Memory-api's topic list, ranks it, holds
  untrusted topics back, and puts the trusted prefix in the live state block.
  `notes.openTopic` expands one topic. Incognito sessions skip the fetch.
- **Work in flight.** Helpers, jobs and commands share one registry. The live block shows
  them together; a prompt preview does not consume the "just finished" flag a real turn
  still needs to see.
- **Memory-api is published** at https://github.com/tochi-mba/Memory-api, public, its own
  repository like every other service. It has a seat in compose, a keyring service token, a
  settings grant on the `memory` namespace, and a row in the compose contract test.
  `/v1/internal` takes two credentials (service token plus the person's memory-api JWT);
  idle current facts in one topic consolidate into a `summary` row. The hub's notes
  client uses that internal surface: `LUCY_MEMORY_API_TOKEN` as Bearer, a minted
  person token as subject proof, never the person-facing `/v1/memory` routes. Memory-api
  `make check` passed on this machine: 277 tests, 100% branch coverage.
- **Foreground delegation and connection consent.** Lucy exchanges through Keyring's
  two-credential internal boundary, exposes subject-bound connection metadata and consent
  tickets, and ships gated music, research, settings, workspace, notes and work packs.
  `GET /v1/setup` overlays vault connection state onto capabilities that name one stored
  credential; a failed inspect stays `unknown` rather than pretending nothing is connected.
  Provider credentials never enter the hub.
- **CLI device sign-in.** Durable, one-time device codes implement the RFC 8628 pending,
  slow-down, denial and expiry vocabulary. Interactive setup opens the browser and polls;
  it never asks for a password.
- **Workspace auto-attach.** Every successful session creation and fork gets a separate
  confined `sessions/<id>` subtree inside the stable account/profile environment. Retries
  reuse that environment and directory instead of consuming the five-environment profile
  quota.
- **M7 approvals and audit.** A gated write parks the turn, survives restart, and resumes
  through `input.approval`. Several writes in one plan become several asks; a subset
  answer leaves the rest pending. `POST /v1/tools/{name}/invoke` is the no-token path
  with the same gate. Grants, refusals, asks, revokes and auto-mode bypasses are
  append-only audit rows.
- **M6 child helpers.** `agents.spawn` starts a real child run of the same loop: a durable
  `agents` row, a journal task, a clean item log, `permission_mode=plan` so the child
  cannot write, and a return capped at 2,000 tokens. Parent mail is drained at the next
  assemble, never mid-tool. A restart marks running helpers `interrupted` and releases
  their journal leases. Spawn is not offered to a helper (depth cap 3). Completion is a
  work notice; the parent reads `work.result`.

- **Workspace bootstrap, fingerprints, and resume orientation.** Every session and fork
  gets `progress.md`, `tasks.json`, and a best-effort git baseline. Reads are numbered
  with file and window digests. Edits walk the application ladder and refuse a stale
  fingerprint. The first assemble of a returning turn re-orients from the journal, tasks,
  git log and a smoke check. Documented in [docs/tools.md](tools.md) and
  [docs/context.md](context.md).
- **M8 MCP, both directions.** RFC 9728 well-known metadata, outbound SSRF, Lucy as a
  dual-era MCP **server** (`POST /mcp`, `server/discover`, skills, tasks gated per
  request), and Lucy as an MCP **client**: hash-pinned `/v1/mcp/servers` plus
  namespaced `mcp.<server>.<tool>` operations gated by `mcp.invoke`. External tools
  never appear under a service name.
- **M3 probe cache and refresh serialisation.** Availability is cached per
  (account, profile, pack) for fifteen seconds. A connect, disconnect, settings write, or
  502 naming a missing credential drops the row. Outbound calls to one sibling audience
  for one person take one lock, so two turns cannot refresh the same grant at once.
- **M6 remainder.** `agents.reopen` continues a finished helper from its transcript.
  `journal.read` / `journal.claim` / `journal.complete` are model-facing tools. Mail
  carries a hop counter, a burst cap, a size cap and dedupe of identical unread steers.
  A caller may declare a JSON Schema; the helper is told to return that object, and a
  miss is named rather than parsed as prose.
- **Remaining session HTTP.** `GET /v1/sessions/{id}/memory` returns the trusted topic
  index (empty when incognito, a notice when Memory-api is down). Workspace GET/POST/reset
  expose the confined directory without a host path. Durable helpers are
  `GET /v1/sessions/{id}/subagents` (process-memory in-flight list stays at `/agents`).
  Helper items are a separate collection from the parent transcript. Executed plan steps
  are persisted so a crash can name what already ran; replay is still refused.
- **Webhooks.** `POST/GET /v1/webhooks` and `DELETE /v1/webhooks/{id}` store HTTPS
  destinations, return the HMAC secret once, and fire `{session_id, turn_id, status}`
  when a turn completes, fails, cancels, or parks. Account erasure deletes them.

## In progress

- M1 still needs end-to-end validation against real providers and the composed family, and
  durable step replay so a crashed in-flight tool can resume instead of being failed.
- M2 keyring exchange and offline grants are implemented in Keyring-api, with the foreground
  broker and device flow integrated in the hub. Full sibling validation and background
  grant scheduling remain before the milestone is closed.
- M4 workspace: safe file primitives are wired. Session creation and forks automatically
  create a confined `sessions/<id>` subtree, seed `progress.md` and `tasks.json`, and try a
  git baseline. Reads return numbered lines and content fingerprints. Edits walk the
  exact→whitespace→fuzzy ladder, refuse a stale fingerprint, and validate JSON/TOML/Python
  before writing. The first assemble of a returning turn re-orients from cwd, the journal,
  `tasks.json`, git log and a smoke check. Linux sandbox runtime validation remains because
  Environments-api cannot import `fcntl` on this Windows host.
- M5 Memory-api: the service is published, the hub shows the topic index every turn, the
  two-credential `/v1/internal` surface exists, Lucy's notes client uses it, and idle
  memories in one topic consolidate into a summary row. Fusion keeps three sections
  separate: persona standing feed, pinned account standing feed, memory retrieval. Their
  scores are never merged. `notes.aboutMe` returns `blocks`, `facts` and `account`;
  `notes.search` is memory only.
- W2–W12 upstream weftai and agentweft changes: pending. W1 is excluded by the plan.

## Known gaps, stated plainly

- A configured OpenAI or Anthropic provider can complete conversational model/tool rounds.
- Settings unavailability **refuses the turn** (503 `settings-unavailable`) when
  `disabled_capabilities` or `approval_policy` cannot be confirmed. Guessing those would
  re-enable something the person turned off. Other lucy knobs are clamped onto
  `TurnPolicy` at prepare time and held for the life of that turn.
- **Hub tests pass on this machine:** 2,019 passed in `tests/hub` at 100% branch
  coverage. Format, lint, mypy, and import contracts are gated by `make check`.
  `python scripts/parity.py --repo lucy-api` is green.
- Nothing has been validated under `make up` with the whole family running, and no real
  model has been called.

## Integration decisions

- Retain existing ADR numbers: 0008 is the Python floor, 0009 the hub location, 0010 the
  ports. Later decisions take fresh numbers rather than overwriting recorded ones.
- Setup manifests are adapters to service documentation. They report deployment readiness
  separately from account connection state, which stays unknown until delegated metadata
  is available.
- No caller token is ever forwarded as a bearer to a sibling. Keyring's exchange receives
  it only as the subject token of an explicit exchange.
- Credentials stay out of transcripts, tool results, logs and persisted session records.
- Commits land on `main` only after `make check` is green in that repository.
- Memory-api is its own public repository, on the owner's explicit instruction. It is
  published only after its own validation and a staged-file review, its checkout stays
  ignored by the hub, and its public URL is listed in `repos.txt`.
