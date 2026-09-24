# Lucy implementation status

The complete requested scope is [the supplied plan](lucy-plan.md). An increment passing
tests does not mean the full plan is complete. This ledger records verified completion and
remaining work, and must survive interruptions.

It is deliberately pessimistic. Anything not verified by a run of `make check` on this
machine is listed as in progress, however finished it looks.

## Verified complete

- **The family runs on Python 3.12 and 3.13**, hub included, and both are gated in CI.
  ADR-0008 records what 3.13 found when it was first gated: a connection each sqlite
  service opened and then dropped when its own startup check refused it. Fixed in all four.
- **The hub is a conforming family service.** `python scripts/parity.py` scores all nine
  repositories, this one among them.
- **The `lucy` command.** Global install through `uv tool install`, status, version, serve,
  setup, connect, doctor, config and talk. `lucy talk` is a client of
  `POST /v1/sessions/{id}/inputs` plus the event stream; hanging up does not cancel the
  turn. Documented in [docs/cli.md](cli.md). Verified working
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
- **Forty-three model providers, three adapters.** A catalogue row per provider -- the
  two native dialects, every OpenAI-compatible host including the Chinese labs and their
  mainland endpoints, the cloud tenants, and eight local runtimes -- with the deviations
  each declares (no tools, JSON-object output only, no reasoning effort, needs a base URL).
  `GET /v1/models` sorts them into `ready` (a listing call answered), `available`
  (configured, unproven) and `unavailable` (with the command that fixes it); `lucy models`
  renders that and `lucy models connect` supplies a key at a prompt, never as a flag.
  Keys are deployment-level (`LUCY_MODEL_KEYS`); per-person keys in the vault are the one
  piece not built. Documented in [docs/models.md](models.md).
- **Work in flight.** Helpers, jobs and commands share one registry. The live block shows
  them together; a prompt preview does not consume the "just finished" flag a real turn
  still needs to see.
- **Watches, and being woken.** `watch.start` says when a workspace file exists or matches,
  a public address answers or matches, or another piece of work ends; `watch.command`
  repeats a command until it exits 0 or matches, under one approval. A watch is work: an
  interval, a lifetime (five minutes by default, an hour at most), a bounded excerpt as its
  result, and expiry as a notice rather than a failure. Work that asked to `wake` -- every
  watch by default, every helper the main thread starts, a command run with `wake: true` --
  opens a turn of its own when it ends and no turn is running, with one harness notice as
  the input; an ending during a turn is held and spent when the turn ends unless the turn
  already read the result. Every ending is a `lucy.work.finished` event; a wake is
  `lucy.work.woke`. Documented in [docs/agents.md](agents.md) and [docs/tools.md](tools.md).
- **Session settings that reach the running turn.** A session carries its own
  `disabled_capabilities` (`["agents"]` is "no helpers in this conversation") beside the
  profile's list. A change to it, to `permission_mode` or to `input_policy` while a turn is
  live is answered with a 409 naming the turn and the two answers: `apply: "now"` lands on
  the running turn at its next round (mode, capability list and the offered plan schema
  are rebuilt), `apply: "after_turn"` holds it and it lands when the turn ends. Documented
  in [docs/sessions.md](sessions.md).
- **Prompt pages.** Every capability's authored markdown lives in
  `src/lucy_api/prompt/capabilities/<id>.md`, beside the stable sections in `defaults/`,
  and is held to the same tests: one page per pack, no orphans, a length ceiling, none of
  the words that describe the wire. No pack carries a prompt as a string constant.
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
  fingerprint. The first assemble of a returning turn re-orients from the journal's latest
  entries, the tasks and the recent commits. Documented in [docs/tools.md](tools.md) and
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
- **Idle archival.** Listing sessions archives conversations whose `updated_at` is older
  than `lucy.session_idle_archive_days` and that have no live or parked turn. Zero days
  means never. A running, queued, or `input_required` conversation is not idle.
- **Approval floor.** `lucy.approval_policy` still asks about destructive writes in
  `auto`, and about spend when it is `spend_and_destructive_ask`. Grants skip the floor;
  the mode does not. `notes.erase` covers `notes.forget` and `notes.unlearn`;
  `workspace.destroy` covers
  `workspace.delete`.
- **Pack documentation.** Help, notes, workspace, music, research, settings, work and
  helpers ship inline markdown the model can read through `help.docs`. MCP skills cover
  the same product names.
- **Sibling defaults.** Omitted `music.play` device ids and `research.search` limits come
  from the person's music and research settings, never from a hard-coded service default
  the model has to guess.

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
  before writing. The first assemble of a returning turn re-orients from the journal's latest
  entries, `tasks.json` and the recent commits. Linux sandbox runtime validation remains because
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
  `TurnPolicy` at prepare time and held for the life of that turn, including fallback
  model, thinking token budget, outward-action confirmation, advertised disconnected
  capabilities, auto-title, slow-turn notice, downstream retries, and helper mail/time
  caps.
- **Hub tests pass on this machine:** 2,456 passed and 18 skipped at 100.00% coverage
  over 14,183 statements and 2,802 branches. Format, lint, mypy, and import contracts
  are gated by `make check`. `python scripts/parity.py --repo lucy-api` is green.
- **The family now comes up together.** `make up` starts nine services and all nine are
  healthy; eight answer `/ready` with 200. See [docs/baseline.md](baseline.md) for the
  first composed run, the four defects it exposed -- none of which `make check` could
  see -- and the readiness-shape divergence across the family.
- **The Linux sandbox is validated under compose.** Environments-api reports
  `sandbox_tier: namespace`, so the M4 caveat below applies only to running the hub
  natively on Windows, not to the composed path.
- **A real model has been called, and ten conversations were held with it.** No API key
  was needed: [clyde](https://github.com/tochi-mba/clyde) serves chat-completions from the
  Claude Code CLI under a desktop subscription, and Lucy reaches it with one entry in
  `LUCY_MODEL_BASE_URLS`. All ten scenarios pass. `docs/baseline.md` records what happened,
  including the six metrics that were the gates for the typed-decision work -- all six are
  now measured.
- **Running it found six defects that 2,522 passing tests did not.** A model spec sent
  where a model id belonged; a plan in a code fence read as prose, so a turn was recorded a
  success while showing the person wire format; a ten-second timeout meant for sibling
  services applied to the model; a failed turn that recorded no reason anywhere; per-turn
  token counts computed every round and never written down; and an approved write that
  never ran. Each is fixed, with a test that fails without the fix. The pattern is one
  thing: a scripted provider serves a reply and never reads the request, so nothing about
  what Lucy actually *sends* was under test.
- **The plan schema is 61% of every prompt** -- 11,495 tokens against 4,462 for the system
  prompt -- and `GET /v1/sessions/{id}/context` reports it as `"tools": 0`, because it
  travels as `response_format` rather than as a message. The hub's own accounting therefore
  sees about a third of what it sends. Anything that reasons about prompt composition,
  including the typed-decision work, should start here.

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
