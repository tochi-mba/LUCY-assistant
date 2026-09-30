# Changelog

All notable changes to the LUCY hub and the family desk are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **The parity check catches a private service however it is written.** It matched a
  private repository's name only with `-` and `_` treated alike, so `Example Tool`,
  `exampletool` and any other name the service goes by -- a URI scheme, a product name --
  passed. Names now match as whole words however their parts are joined, and a private
  repository lists its other names under `[tool.lucy] also-known-as` in its own
  pyproject.toml, which the check reads from the local checkout. It also reads the
  top-level build files, `pyproject.toml`, `Makefile`, `Dockerfile` and `.env.example`,
  whose comments it used to skip. See [docs/private-repos.md](docs/private-repos.md).
- **An approved call runs with the steps it reads from.** A plan is checked whole before
  any step runs, so a plan whose write needs a person parks before its reads have run, and
  the approved call used to run on its own, with a reference such as `$found` left
  unresolved. Each approval now records its call's step and every step it reads from, and
  the resumed turn runs them together under their own ids; a call reading from a step the
  person refused does not run, the model is told why, and its one-time grant goes with it.
  A resumed plan that parks again, because the mode changed after the answer, gives its
  approved calls back to run with the next answer.
- **A plan the executor would refuse is never asked about.** The permission gate saw a plan
  before the executor checked it, so a plan with a repeated step id, a reference to nothing
  or a step without an `op` could be asked about and approved, then refused or replayed as
  something else. A plan is now checked the way the executor checks one first; what cannot
  run goes back to the model to repair, and the gate reads a step's operation only from
  `op`.
- **`music.play` and `music.queue` take the track `music.find` found.** They took only a
  `uri` string, so the plan a model naturally writes -- find, then play what was found --
  sent the literal text `$found` as a URI. They now take `track`, a reference to
  `music.find`'s result, as the tools guide always showed; a `$` reference given as a `uri`
  is refused with the fix, and the capability page shows find and play in one plan.
- **A queue of several tracks answers per track, inside the step's ceiling.** The service
  takes one track per queue command, so a six-track queue is six commands in a row; one
  refused part way ended the step with that one failure and hid which tracks had been
  queued, and six slow ones ran past the step's ceiling, so the step timed out and nothing
  was reported. `music.queue` now says for every track whether it was queued and, if not,
  why; it stops before the next command would run past the ceiling and says how many were
  left to queue in a new step. A command accepted but not confirmed is noted as before.
- **A reference in `uri` is refused before the plan runs, whitespace and all.** The check
  looked at the first character, so `" $found "` was not seen as a reference and went to the
  music service as the text written, and a bare `$found` was refused only once the step ran.
  The `uri` field's schema now refuses anything that begins, after optional whitespace, with
  `$`, and its description says a reference goes in `track`; the operation strips
  whitespace before it looks, as the second line of defence, and a padded uri is the uri
  inside.

### Changed

- **The music audience is configuration.** `LUCY_MUSIC_API_AUDIENCE` sits beside
  `LUCY_MUSIC_API_BASE_URL`, defaulting to `spotify-api`. An audience names exactly one
  service -- keyring refuses a credential read whose token was minted for anybody else -- so
  a second implementation of the music contract is minted tokens for its own name, and a
  private one adds that name under `exchange_audiences` in the gitignored
  `scripts/genenv.local.json`. A short-lived shared `music-api` audience broke every
  Spotify credential read and was withdrawn.
- **A renamed variable in `.env` fails at startup with its new name**, rather than with
  pydantic's "extra inputs are not permitted".

### Added

- **An eval scenario can change things between turns.** Exploratory conversations, held
  with `lucy eval run --suite <folder>`, often need the world to move between two things
  the person says. A turn's `[[turns.before]]` steps run once the previous turn has come to
  rest: an `op` through the invoke route, as seeds and verify steps run; a `host` command on
  this machine, through the shell; or a `wait_seconds` pause. A step that does not end as
  written leaves its turn unsent and the scenario an `error`, and both reports show every
  step and how it ended. A run whose scenarios have a `host` step refuses to start without
  the new `--allow-host`, and `--dry-run` lists every command. See
  [docs/evals.md](docs/evals.md#holding-an-exploratory-conversation).
- **`workspace.script`: a scratchpad for Lucy's own scripts.** A quick calculation, a check
  or a one-off transformation took `workspace.write` and then `workspace.run` -- two
  operations and, in `ask` mode, two approvals -- and left the script among the person's
  files, where `git status` reported it as one of their changes. `workspace.script` writes
  a short Python or bash script to `.scratch/` and runs it in one call, under the permission
  that already covers commands, with the code on the approval card. A named script is
  rewritten and run again; an unnamed one is named by its code. The folder's own ignore
  file ignores everything in it, itself included, so scratch work never shows as a change.
  See [docs/tools.md](docs/tools.md#a-quick-calculation-in-one-call).
- **The eval harness halts a turn at the first thing wrong.** A watchdog reads the
  transcript on every poll and stops the turn at a failed step, an error in the transcript,
  the same failure twice, or an ask for a call already answered -- before answering it
  again -- then cancels it and ends the scenario with the rule and its evidence. Each step
  and ask is printed as it lands. A turn names failures it expects the model to recover
  from in `allow_errors`. See [docs/evals.md](docs/evals.md#the-watchdog).
- **`lucy eval`: conversation regressions, on demand.** Every defect found by talking to
  Lucy through a real model was invisible to the unit suite, because the scripted
  provider never reads the request. `lucy eval run --model clyde:haiku` (or `make evals
  MODEL=clyde:haiku`) holds a constant list of real conversations against a running hub,
  with any `provider:model` it can use, and checks what the hub *recorded* after each turn:
  which operations ran and which must not have, what the reply says, whether wire format
  leaked, and -- through the invoke route -- whether a file the model claims to have
  written exists. It answers approvals as each turn says, skips a scenario whose
  capability is not ready, archives every session, grants only for the session a step is
  for, and writes `report.json` and `report.md` with failures first; `--compare` lists
  regressions and fixes against an earlier run, and `--repeat` shows flaky checks as pass
  rates. The shipped suite is ten prompts, each naming the defect it guards. It never runs
  in CI or `make check`. See [docs/evals.md](docs/evals.md).
- **The context engine.** What Lucy knows when it answers is now something the codebase
  states rather than something that emerges. The prompt is five zones ordered by how often
  they change, and the block that carries the state of the world is rewritten every turn
  and read **last**, so that keeping Lucy current does not end the cached prefix and charge
  full price for the whole conversation. That block tells the model the date, where the
  session stands, how full its own window is, which child agents are running and what they
  are for, which finished since the last turn, the shared task journal, the memory topic
  index, the workspace, what changed about its capabilities, what is waiting on a human,
  and what has been failing repeatedly. Five bands are budgeted independently, so a flood
  of tool output can never evict the person's pinned context, and every trim, drop and
  omission is confessed in the text the model reads. Compaction is a projection over an
  append-only transcript, never an edit, so a bad summary can be regenerated. A source that
  is down costs its own group and nothing else. See [docs/context.md](docs/context.md).
- **Memory is organised into topics.** A flat list of facts cannot be summarised and tells
  a model nothing about what it knows. Memories now cluster into named topics, and what
  travels in the context every turn is the index -- title, one-line summary, count, recency
  -- with the contents expanded only for the topic the model decides it needs. A topic made
  entirely of unconfirmed memories never reaches the index, because its title came from
  untrusted content.
- Global client installers, `scripts/setup.sh` and `scripts/setup.ps1`, that work from
  any directory, preserve argument boundaries, support dry runs, and stop before setup
  if installation fails.
- `lucy setup` for hub, family or remote configuration; `lucy config` for redacted
  inspection; `lucy doctor` for actionable checks; and `lucy connect` for capability
  discovery that reports when the running hub does not support connection setup.

- **The hub.** `src/lucy_api/` is the beginning of Lucy: configuration that refuses an
  unknown `LUCY_*` variable at startup, a keyring token verifier with the family's
  401-versus-503 split, `GET /healthy` and `GET /ready`, and `GET /v1/me`. It is held to
  the same gates as every sibling, and `python scripts/parity.py` now scores this
  repository too. See [ADR-0009](docs/adr/0009-the-hub-lives-here.md).

### Fixed

- A direct tool call scoped to a session now reaches that session's workspace.
  `GET /v1/tools?session_id=` and `POST /v1/tools/{name}/invoke` with a `session_id`
  built their context without the session's workspace, which only a turn attached, so
  the workspace probed as "no workspace is attached" and a session's own files could not
  be listed, read or written from either route. Found by the eval harness's contract test.

### Changed

- **Breaking:** the family floor is **Python 3.12**, and CI gates 3.12. Python 3.13 is
  declared supported and remains an optional local test run.
  [ADR-0008](docs/adr/0008-python-3-12-floor.md) records why: `weftai`, which the hub
  depends on, requires 3.12 and uses PEP 695 type parameters that do not parse on 3.11.
  `scripts/parity.py` enforces the new floor, and its check descriptions are now rendered
  from the same constants it checks against, so the two cannot drift apart.
- `pytest.ini` was folded into `pyproject.toml`, which the repository now has because it
  ships a package.
