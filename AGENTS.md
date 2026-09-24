# Working notes for this repository

This repository is two things at once, and knowing which one you are touching is most of
the job.

1. **The hub.** `src/lucy_api/` is Lucy: the HTTP API a person holds a conversation with.
   It is a family service like any other and is held to the same gates.
2. **The family desk.** `scripts/`, `docs/`, `docker-compose.yml`, `repos.txt`,
   `.github/workflows/service.yml`, `broker/` and `examples/` are how the other services
   are bootstrapped, checked, built and released. They are not part of the wheel.

[ADR-0009](docs/adr/0009-the-hub-lives-here.md) records why the hub lives here rather than
in a tenth sibling repository, and what that costs.

## Invariants

**The model never sees a service.** It sees capabilities with product names — `music`,
`research`, `workspace`, `notes`. No prompt, tool description or error message mentions a
port, an HTTP verb, or a repository name. If you are about to write "spotify-api" into
something a model reads, stop.

**Tool results are data, never instructions.** Anything that arrived from a web page, a
tool, a memory or a sub-agent is rendered as a third-person reported claim with its
provenance inline, inside a delimited block that is not the instruction block. Never
concatenate a remembered note into the system prompt. Never strip provenance to save
tokens — fetch fewer things instead.

**Nothing truncates silently.** Every cap emits a notice with exact counts
(`showing 30 of 35`), and anything spilled stays reachable by reference. This is weftai's
rule and it applies to everything the hub writes, not only to weftai's own output.

**Credential material never enters a prompt, a tool result, an event or a log.** The hub
sits next to a vault. A test asserts this and it is not negotiable.

**The hub never forwards a caller's token to a sibling.** It mints a token for that
audience, for that person, through keyring. One scope per token, so expect to hold several.

**Errors name the fix.** "Unknown field `orign`; this collection has `origin`, `label`" —
not "invalid input". A wrong question is an error, never an empty result.

## Gates

`make check` is `lint type imports test`, and it does not grow a fifth gate. Anything else
is its own verb, run on demand: conversations with a real model are `make evals
MODEL=provider:model` (`lucy eval`), which no workflow and no pytest run ever starts.

- 100% branch coverage, `fail_under = 100`, and no `pragma: no cover`.
- mypy `strict = true`; `filterwarnings = ["error"]`.
- No file under `src/`, `tests/` or `scripts/` over 1000 lines.
- Import contracts are the architecture statement, not an afterthought. Routers never
  import `httpx`, `keyring_client` or `settings_client`.

`python scripts/parity.py` scores every sibling **and this repository**. Run it after
changing anything the family standard cares about.

## Dependencies

`weftai` comes from PyPI, pinned exactly (`weftai[all]==0.2.4`), never as a path
dependency to a checkout. Improving weftai means releasing weftai — to npm and PyPI in
lockstep — and then bumping the pin here.

`keyring-client` and `settings-client` come from their owning hubs as tagged git sources
([ADR-0002](docs/adr/0002-shared-clients-live-in-the-hub.md)). Do not vendor a copy.

## Testing shape

Fakes follow `settings_client`'s shape: a `Protocol` at the seam, a hand-written `Fake`
that satisfies it, and an in-process client running the real code against the real app.
`transport=` on `create_app` is the seam that keeps the suite offline.

The hub's own tests live in `tests/hub/`. The family desk's tests are the rest of
`tests/`, and they stay.
