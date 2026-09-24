# Changelog

All notable changes to the LUCY hub and the family desk are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
