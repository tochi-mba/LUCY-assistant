# Optional Laya decisions

Lucy uses the published Weft 0.4.0 decisions contract and
`weftai.providers.laya.LayaDecider`. The matching npm adapter is
`@weftai/providers/laya`. Both use a long-running Laya HTTP service.
Installing Lucy does not install PyTorch or download checkpoints.

The feature starts **off**. Enabling it starts in **shadow mode**: suggestions are
measured but do not change the tools, memory ordering, or recovery notices.
There is no measured claim of model accuracy or end-to-end latency improvement.

## What it does

- Capability selection asks atomic relevance questions about available capabilities.
  At confidence 0.85 or higher, at most two suggestions are added to the loaded tools.
  Existing tools stay loaded, ordinary discovery stays available, and session recency
  is unchanged. A selected write still goes through the ordinary permission gate.
- Memory relevance promotes relevant trusted topics before the existing topic limit.
  It preserves all topics in storage, explicit keys, trust, and access through
  `notes.search`. A changed selection reports the exact omitted count. Relevance order
  survives live-state rendering. Incognito never sends memories to the decision service.
- Recovery is separately off by default. After consecutive failed rounds it can add
  one advisory reconsideration notice at confidence 0.9. It cannot stop a turn, drop a
  tool, execute an action, or weaken deterministic repetition and approval rules.

Topic assignment remains owned by Memory-api. The unused local decision-based assignment
helper has been removed rather than creating a second assignment implementation.

## User controls

All controls are in the `lucy` settings namespace, managed through Lucy's existing
settings surface and Settings-api. Settings apply when the next turn is prepared.

- `decisions=false`: master switch. Off makes no Laya calls.
- `decision_shadow_mode=true`: measure without applying suggestions.
- `decision_capabilities=true`: capability preloading, subject to the master switch.
- `decision_memory=true`: memory relevance, subject to the master switch and privacy rules.
- `decision_recovery=false`: advisory recovery judgments.
- `decision_timeout_ms=1000`: per-call ceiling, bounded to 50–5,000 ms.
- `decision_max_per_turn=8`: call budget, bounded to 1–32.

Enable the master first, inspect shadow results, then explicitly turn shadow mode off
for the enabled uses. Disable individual uses independently. Helpers currently use the
ordinary deterministic paths and make no decision calls; the main turn's budget cannot
multiply through child agents. Identical successful judgments are cached only in that
main turn, using a digest rather than storing input text in the cache.

## Deployment

For Docker, set a new `LAYA_API_KEY` in your shell, then:

```sh
docker compose -f docker-compose.yml -f docker-compose.laya.yml up -d --build
```

The optional overlay builds Laya 0.3.20, persists its model cache, and uses an internal
endpoint with bearer authentication. It exposes no inference port on the host.
First startup downloads the selected checkpoint; `LAYA_MODELS` defaults to `english`.
Use `english,multilingual` to preload both. CPU thread count defaults to two and can be
set with `LAYA_THREADS`. No optional sidecar is started by the default compose file.

For a host installation, use a separate environment with `laya[serve]==0.3.20`, set
`LAYA_API_KEY`, and run `python scripts/laya_server.py`. Its default address is
`127.0.0.1:8010`. Configure Lucy with:

```dotenv
LUCY_LAYA_BASE_URL=http://127.0.0.1:8010
LUCY_LAYA_API_KEY=<same key as LAYA_API_KEY>
LUCY_LAYA_MODEL=
LUCY_LAYA_TIMEOUT_MS=5000
LUCY_LAYA_MAX_CONCURRENT=2
```

Empty model lets Laya route. An empty endpoint installs a no-op decider. The operator
timeout is an upper bound; the per-user turn timeout can be shorter. Treat the endpoint
as a trusted deployment component: it receives sanitized request text and, when enabled,
trusted topic summaries. Credential-pattern scrubbing is a backstop, not a guarantee
that arbitrary secrets embedded in prose can be identified.

Use the supplied guarded server. Upstream's server can truncate at its checkpoint's
window; this wrapper checks tokenized instructions, each option, and the complete state
before inference and refuses anything that would be truncated. Lucy falls back.
The adapters also refuse oversized state and responses. Neither transport byte limits
nor the guarded server enlarge a checkpoint's context window.

## Verification and rollout

Offline tests exercise disabled, shadow, and live application using a scripted decider;
they test the first-round tool schema, permission enforcement, memory rendering,
consecutive failures, input guards, cancellation, caching, and HTTP wire compatibility.
These verify behavior, not learned-model quality.

With the guarded service running, use the separate live smoke command:

```sh
uv run python scripts/eval_laya.py --url http://127.0.0.1:8010
```

It reports aggregate correctness, acceptance, fallback counts, and latency on nine
synthetic cases, including a multilingual request. It reads authentication from
`LAYA_API_KEY` and does not print prompts or credentials. This small corpus is not a
release-quality accuracy benchmark. Use representative consenting traffic in shadow
mode before changing defaults.

Compare baseline, shadow, and applied runs for task completion, first-round capability
availability, unnecessary tools loaded, memory relevance, model rounds, and wall-clock
latency. An avoided discovery round is a possible benefit, not a guaranteed saving.
`lucy.decision.made`, `.skipped`, and `.disagreed` contain bounded decisions and timing,
never request or memory text. Disagreement means a different suggestion, not proof that
the suggestion was right. Empty/malformed transport results retain ordinary behavior.
