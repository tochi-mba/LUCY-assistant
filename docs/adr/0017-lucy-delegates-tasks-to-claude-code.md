# ADR-0017: Lucy delegates whole tasks to Claude Code on the host, one card at a time

**Status:** accepted (2026-10-08). Built: the bridge (`src/lucy_coder`), the three
`lucy.claude_code_*` settings, and the hub's `coder` capability.

## Context

The owner asked that Lucy be able to "connect to Claude Code": when the person turns it on,
Lucy hands a task to a real Claude Code session, keeps track of it, checks on it, and sends
it follow-ups. This is not clyde. clyde is the model provider behind Lucy's own turns; this
is Lucy delegating work to a separate program that acts on the person's computer.

[ADR-0014](0014-claude-code-tools-through-a-bridge.md) considered a different shape --
Claude Code as the provider, calling Lucy's operations mid-turn -- and stays *proposed*. It
rejected host `Bash` *in that shape* because the model there would act ambiently, inside a
conversation, with no human looking at each act. Its Phase-0 prerequisite (an assistant may
never loosen its own settings; the hub enforces `agent_writable`) was built on 2026-10-06/07
and is what this decision stands on.

Three facts decide the shape:

* **The hub runs in Docker and cannot start host processes.** Claude Code lives on the host
  with the person's own login and subscription.
* **The CLI is scriptable headless** (verified 2026-10-08 on 2.1.280): `-p` with
  `--output-format stream-json --verbose` emits one JSON record per line and ends with a
  `result` record carrying cost, turns, `is_error` and `subtype`; `--session-id` names a
  session and `--resume` continues it; `--permission-mode` and `--max-budget-usd` bound it.
  There is no `--max-turns`. `--resume` on a *busy* session does not refuse -- it runs a
  second turn concurrently on the same transcript.
* **A loopback-bound host service is reachable from the hub** through
  `host.docker.internal` on Docker Desktop, so the bridge never has to listen wider.

## Decision

**A delegation primitive, not ambient tools.** A task is a written brief, shown to the
person on an approval card, run in one folder they listed, at a run level they chose, and
tracked as work. Two pieces:

1. **The bridge** (`src/lucy_coder`, `make coder`, 127.0.0.1:8012): host-run deployment glue
   in this repository, like the hub itself (ADR-0009) and clyde -- not a sibling repo. It
   wraps `claude` as subprocesses; keeps one durable SQLite row and one JSONL transcript per
   task under `var/coder/`; runs at most two sessions at once (the owner's number) with the
   rest queued; queues follow-ups inside a task so one session never runs two turns; kills
   the process tree on cancel or on its 45-minute wall clock; and turns every ending into a
   sentence on the row -- missing CLI, signed out, session limit, budget spent, timed out,
   cancelled, or interrupted by a bridge restart (failed, resumable). It believes exactly
   one thing: a keyring token minted for the `coder-api` audience.

2. **The `coder` capability** (`packs/coder.py`): `coder.delegate`, `coder.message`,
   `coder.read`, `coder.list`, `coder.cancel`. Each delegated turn is a piece of work
   (`Kind.job`) that polls the bridge every five seconds until the task leaves
   `queued/running`, narrates hub-written counter sentences in the live block ("14 tool
   uses, last: Edit" -- never Claude Code's own words, which would reach the prompt
   unfenced), wakes the session when the turn ends, and carries the person's standing
   consent and quiet hours like every other waking work. Everything it returns is
   `untrusted`: another program's account of the person's machine is data.

**Three switches, none of them Lucy's.**

| Switch | Who | Where | Off means |
|---|---|---|---|
| Operator | whoever runs the hub | `coder_api_base_url`, set only in the gitignored `docker-compose.local.yml` | `not_configured`: the capability is absent |
| Person | the person | `lucy.claude_code_delegation`, `lucy.claude_code_directories`, `lucy.claude_code_run_level` | `disabled`, with a sentence telling Lucy whose switch it is |
| Per task | the person | an approval card for every `coder.delegate` and `coder.message` (`each_call`) | nothing runs |

The three settings are `agent_writable: never` on both sides of the fence -- Settings-api
declares it and the hub's own catalogue wins over a stale one -- because a model that could
loosen them could hand itself the person's computer. On an outage they land conservative:
off, no folders, no looser than `edits`. A folder is allowed by exact membership after
normalising case and separators: never a prefix (`C:/repos` must not allow
`C:/repos-secret`) and never resolved on the hub's own disk, which cannot see the host's.

Run levels map to `--permission-mode`: `plan` → `plan`, `edits` → `acceptEdits` (the
default), `full` → `bypassPermissions`. The card names the level each task will run at.

## Alternatives rejected

* **Run Claude Code inside the sandbox.** The person's login, subscription and files are on
  the host; a sandboxed copy could do none of what was asked.
* **Claude Code as an MCP server Lucy calls.** It would still need a host process and a way
  in from the container, and would trade a durable task row for a request that dies with
  the turn -- "check on yesterday's refactor" would have nothing to check.
* **A new sibling repository.** It is deployment glue for one machine, not a service the
  family ships; the hub itself lives here for the same reason.
* **Signals instead of polling.** `signal_base_url` is one global value a host process
  cannot resolve; a per-sibling signal URL is a change of its own. Polling costs one small
  GET every five seconds while a task lives and matches how watches work.

## Consequences and edges

* **Delegations spend the same Claude subscription clyde uses.** A session limit is an
  honest task failure with the CLI's own sentence; it never hangs.
* **A hub restart does not resume the pollers** (`RESUMED_AFTER_RESTART` covers helpers and
  subscriptions only). The bridge keeps running the task, and `coder.list`/`coder.read` find
  it from any conversation; only the finish notice is lost.
* **The bridge restarting mid-turn** fails the row as resumable: Claude Code kept its
  session file, and a message continues it.
* **Linux hosts** would need `extra_hosts: host-gateway` for `host.docker.internal`, and a
  process-group kill instead of `taskkill /T` (the runner names the seam).
* **Plan mode explores before it answers**: a trivial plan-only task cost $0.22 in the
  spike, so the per-turn budget ($1 by default) is set for real work, not for "pong".

## What would change our minds

* If people want Lucy to *suggest* delegating unprompted, the capability page's "only when
  asked" rule is the line to revisit -- the card would still be the guard.
* If two concurrent sessions prove too few, `CODER_MAX_LIVE_TASKS` is the knob; the hub
  needs no change.
* If the CLI grows a turn cap or refuses a busy `--resume`, the bridge's own wall clock and
  follow-up queue become belts as well as braces.
