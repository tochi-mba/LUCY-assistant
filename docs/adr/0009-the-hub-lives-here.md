# ADR-0009: the hub lives in this repository

**Status:** accepted
**Amends:** [ADR-0001](0001-meta-repo-not-monorepo.md), and CONTRIBUTING's "not a ninth
service"

## Context

The family is eight services and no front door. The assistant — the thing that runs a
model, holds a conversation, decides which capabilities a person actually has, and calls
the other eight on their behalf — had nowhere to live.

ADR-0001 says this repository clones the services and contains none of them, and
CONTRIBUTING says the same: *"a teaching example under `examples/` (not a ninth service)…
Not copies of the eight services."* Both were written about **service code**, when every
service was a peer. The hub is not a peer: it is the thing the family is *for*, and the
name on this repository is already its name.

The alternative considered and rejected was a tenth sibling repository, `Lucy-api`, cloned
beside the others. It preserves ADR-0001 exactly and costs nothing structurally. It was
rejected because the product's front door would then be the one thing not present when you
clone the repository named after the product.

## Decision

The hub is `src/lucy_api/` in this repository, on port 8000, and this repository is held to
the family standard like any service: the hello-api skeleton, the Makefile verbs,
`make check` as exactly `lint type imports test`, 100% branch coverage, mypy strict, import
contracts, the documentation set, and a `LUCY_`-prefixed configuration that refuses an
unknown variable at startup.

ADR-0001's substance survives intact and is restated here: **the eight services remain
eight git repositories**, cloned beside this file, gitignored, never committed here. This
is not a monorepo. It is a repository with a product in it and a desk beside it.

`scripts/`, `docs/`, `docker-compose.yml`, `repos.txt`, `broker/`, `examples/` and
`.github/workflows/service.yml` remain the family desk and are not part of the wheel;
`[tool.coverage.run] source = ["lucy_api"]` is where that line is drawn.

## Why

**The name is the argument.** Somebody who clones `LUCY-assistant` should be able to talk
to Lucy. Anything else needs a paragraph of explanation, and a design that needs a
paragraph to explain where the product is has already lost.

**The reasons for ADR-0001 do not apply to the hub.** A vault, a sandbox and a headless
browser do not share a release cycle, a threat model or a dependency tree — that is why
they are separate. The hub shares all three with the desk that describes it: they are
released together, they are read together, and a change to the family standard is usually a
change to both.

**The gates are the safeguard.** The risk in putting product code here is that the
repository stops being held to the standard it enforces. Scoring this repository with the
same checker it runs against the others removes that risk mechanically rather than by
intention.

## What it costs

`scripts/parity.py` now scores this repository, which required teaching it to look at a
root that is not a sibling. The meta CI gained the full service gate set by calling its own
reusable workflow (`uses: ./.github/workflows/service.yml`). `docker-compose.yml` builds
`.` for the first time. `pytest.ini` became `pyproject.toml`. The repository grew a
`Dockerfile`, a `CHANGELOG.md`, an `AGENTS.md` and a documentation set it did not need when
it held no code.

Anyone who assumed "this repository contains no Python package" — a script, a CI job, a
mental model — has to stop assuming it.

## What would change our minds

If a second product joined the family, or if the hub's release cadence diverged sharply
from the desk's, the argument above evaporates and `Lucy-api` becomes a tenth repository.
The skeleton is deliberately the same as every sibling's, so that move stays cheap.
