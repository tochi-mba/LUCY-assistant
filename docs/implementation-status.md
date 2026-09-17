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
  Documented in [docs/context.md](context.md).
- **Memory-api is published** at https://github.com/tochi-mba/Memory-api, public, its own
  repository like every other service. 238 tests at 100% branch coverage, CI green on its
  first run. It has a seat in compose, a keyring service token, a settings grant on the
  `memory` namespace, and a row in the compose contract test.

## In progress

- M1 durable sessions: the schema, the store and the SQLite worker thread exist. They are
  not yet wired to any route, and until they are the coverage gate counts them as untested.
- M1 context engine: budgeting, the live state block, framing, the scrubber, the prompt
  sections and the memory topic index are being built against the contract above.
- M2 keyring exchange and offline grants: in progress in Keyring-api. The broker and the
  device flow still need hub integration.
- M3 capabilities: setup readiness discovery exists. Execution, consent and refresh pending.
- M4 workspace: file primitives in progress in Environments-api; the hub's pack pending.
- M5 Memory-api: the service is complete and published. What remains is the hub's side:
  fusion across Memory, User-api and Persona-api, and the background consolidation pass
  that writes better topic titles and summaries.
- M6 agents and the journal: pending. The single-agent loop and compaction land first.
- M7 permissions, approvals and audit: pending.
- M8 MCP, both directions: pending. Revision 2026-07-28 confirmed against the published
  specification.
- M9 compaction, usage, files and artifacts, erasure, streaming codecs: pending.
- W2–W12 upstream weftai and agentweft changes: pending. W1 is excluded by the plan.

## Known gaps, stated plainly

- The turn loop does not exist yet, so Lucy cannot hold a conversation. Everything above it
  is foundation.
- The context engine is not yet wired to a route: `GET /v1/sessions/{id}/context` needs
  the session surface, which is M1.
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
- Nothing is committed, pushed or published as a side effect of implementation.
- Memory-api is its own public repository, on the owner's explicit instruction. It is
  published only after its own validation and a staged-file review, its checkout stays
  ignored by the hub, and its public URL is listed in `repos.txt`.
