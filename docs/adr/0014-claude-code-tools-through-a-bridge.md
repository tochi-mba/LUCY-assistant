# ADR-0014: Claude Code may use tools only through a turn-scoped Lucy bridge

**Status:** proposed (2026-09-24). Nothing here is built. Phase 0's prerequisite is.

## Context

clyde serves Lucy's model calls from the Claude Code CLI under a desktop subscription. It
removes every one of Claude Code's own tools (`--tools ""`, `--disable-slash-commands`, an
empty strict MCP config), runs one turn per call, and proves at startup that nothing loaded.
Lucy runs the agent loop: the model answers with a plan, and Lucy executes it through its own
permission gate, in the session's sandboxed workspace, recording every step.

The owner wants a per-person choice to let Claude Code use tools itself, only when the
session's model is clyde, and only if those tools "work in tandem with our Lucy system": the
same workspace, the same permissions and approvals, the same transcript.

Three facts decide what that can mean:

* **The workspace is not on the machine clyde runs on.** It lives on a named Docker volume
  inside the Linux VM, at `/var/lib/envapi/accounts/<acct>/<profile>/<env>/workspace/...`.
  There is no Windows path clyde could hand Claude Code as a working directory.
* **Claude Code's built-in `Bash` would run on the host**, as the owner, outside Lucy's
  sandbox, its per-session confinement, Environments-api's audit and quotas, and every
  permission mode. clyde listens on `0.0.0.0`. A prompt-injected model with host `Bash` is
  remote code execution on the owner's desktop.
* **Claude Code can take tools over MCP.** With `--mcp-config` naming one server and
  `--strict-mcp-config`, the only tools it can call are that server's. And
  `--permission-prompt-tool` (present, undocumented in `--help`) can delegate its permission
  decisions to an MCP tool.

Lucy's existing `/mcp` server cannot be that server. It is account-scoped with the person's
JWT, skips turn preparation (no workspace, no grants, no policy -- disabled capabilities are
not even applied), cannot open approvals, writes nothing to the transcript, and exposes tools
that start more conversations.

## Decision

**Literal built-ins stay off, always.** What the setting enables is a *bridged tool loop*:
Claude Code keeps `--tools ""`, and its only MCP server is a new, turn-scoped Lucy endpoint
whose tools are the operations bound for that turn. Claude Code runs its own multi-turn tool
loop inside one model call; every call it makes is one of Lucy's operations, gated by
`Capabilities.execute` with the turn's own context, recorded as a `tool_result` item and a
`steps` row as it happens, and streamed as `lucy.tool.*` events.

* **Authority is a bridge token, not the person's JWT**: 256 bits, held hashed in memory,
  bound to account, session, turn and round, revoked when the round ends. Its tools carry no
  `session_id` argument; nothing crosses sessions; nothing starts another conversation.
* **An approval needed mid-call ends the round.** Lucy's approval state machine requires the
  turn to be parked before an answer is accepted, and a parked turn cannot also be running a
  model process. So the bridge records the ask, stops the call, and the turn parks the
  ordinary way; the next round starts a fresh process that re-issues the call once allowed.
  The resumed notice must stop claiming "nothing has happened yet": in a bridged round, the
  calls before the ask did run.
* **No same-round fallback after a bridged call executed**, and no mangled-reply retry: both
  would replay side effects.
* **Three switches, all required.** clyde's operator (`CLYDE_BRIDGE_ENABLED`, a pinned
  `CLYDE_BRIDGE_URL` the request can never name, and a caller secret compared in constant
  time); Lucy's operator (`LUCY_CLYDE_BRIDGE_ENABLED`); and the person
  (`lucy.claude_code_tools`, `off | bridged`, profile-scoped, default `off`, **never** writable
  by an assistant, copied onto a session at creation only when its model provider is
  `clyde`, and changeable per conversation by the person).
* **clyde checks every bridged run's `system/init`** before the first model response: tools
  must be exactly `mcp__lucy__*`, the only server `lucy`, connected -- or it kills the run.
  The server entry sets `alwaysLoad: true`; without it, MCP tools can be deferred behind tool
  search, which `--tools ""` removes -- the same failure that leaked `TodoWrite` through the
  old deny-list.

## Consequences

* A person gets Claude Code's own tool use -- its loop, its parallel calls -- with Lucy's
  operations in place of `Bash`, `Read` and `Edit`. The setting's text must say exactly that.
* Bridged rounds cost more tokens: results enter Claude Code's context instead of being
  referenced by `$ref` in a plan.
* Lucy stays one process: the bridge registry is in memory.
* clyde needs a separate, smaller concurrency limit for bridged runs, because web-search also
  reaches clyde and a bridged run calling `research.*` must not deadlock on its own slot.

## Prerequisite (Phase 0)

The "an assistant may never change this setting" rule must be enforced before this setting
exists, or a model in `auto` mode could switch the bridge on for itself. Settings-api declares
`agent_writable` and says the hub must apply it; until 2026-09-24 nothing serialised or
enforced it. That fix is in progress separately and is the first thing this depends on.

## Plan

0. Spikes against a local stub MCP server: the exact MCP method sequence Claude Code 2.1.280
   sends; `alwaysLoad` with `--tools ""`; `--allowedTools mcp__lucy` under `dontAsk`;
   stream-json `result` parity and kill-on-disconnect leaving no stray processes on Windows;
   `--no-session-persistence`; whether a loopback-bound clyde is reachable through
   `host.docker.internal` (which would end its LAN exposure outright).
1. clyde's bridged path, off by default: its own argv builder and lockdown check, a streaming
   runner that kills on bad init, disconnect or cancel, keep-alives, the three operator
   settings and their `/ready` check. Every existing test unchanged with the switch off.
2. Lucy's bridge: the token registry, a turn-scoped MCP dispatcher over the bound operations,
   loop and supervisor integration, the setting and the session field, a provider-neutral
   prompt section for "operations reach you as callable tools this turn".
3. Progress and cleaner stops: narration as reasoning; `--permission-prompt-tool` only if the
   spike shows its `-p` behaviour is sound.
4. Optional inline approvals with a bounded hold, falling back to parking.

## Alternatives rejected

* **Point Claude Code at `/mcp` with the person's JWT**: wrong scope, no workspace, no
  approvals, no transcript, and the full-account token on the host.
* **Bind-mount the environments volume to the host** for `Read`/`Edit`/`Write`: every
  account's workspace on the owner's disk, symlinks resolving differently on each side, the
  sandbox's audit and quotas skipped, and commands still on Windows.
* **Check out a copy of the workspace to the host and patch it back**: stale the moment a
  sandbox command changes a file; secrets and binaries on the host disk; only a diff in the
  transcript.
* **Run Claude Code inside the sandbox**, where real built-ins would act on the real
  workspace: the subscription credential would sit in a sandbox tier that is organisation, not
  a boundary, readable by any command the model runs. Revisit with the namespace tier and a
  credential proxy.
* **`--permission-prompt-tool` as the primary gate**: undocumented and unmeasured in `-p`
  mode. Lucy's in-call gate is sufficient; the prompt tool is an improvement, not the defence.

## What would change our minds

A stronger sandbox tier with a credential proxy (making real built-ins in the sandbox safe),
or Claude Code documenting a remote tool-execution protocol that lets a host run its
built-ins elsewhere.
