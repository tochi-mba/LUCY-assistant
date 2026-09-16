# Contributing to the LUCY-assistant family

This repository is the family desk: the checklist, the ports, the CI workflow the
services call, and the bootstrap that clones the eight checkouts. It is not a monorepo
and it does not contain service code. Each service is its own git repository; see
[ADR-0001](docs/adr/0001-meta-repo-not-monorepo.md).

Read [AGENTS.md](AGENTS.md) in the service you are actually changing. That file is the
source of truth for *that* process. This file is the source of truth for *the family*.

## Getting set up

```bash
bash scripts/bootstrap.sh
# or: pwsh scripts/bootstrap.ps1
```

Idempotent. Existing checkouts are left alone — no `git pull`, no `fetch`, no
`reset`, no `checkout`. Docker is reported, never installed. Pass `--no-install` to
skip `make install`. Pass `--dry-run` to print the plan. `--check` runs `make check`
per checkout and prints a table; Environments-api is marked `needs Linux` when the
host is not Linux.

Bootstrap runs `gh auth login` (browser or a pasted token) when you are not signed in
and makes `gh` git's credential helper, so private checkouts and `uv`'s client fetches
use your account. That login is what you use to run Lucy locally. CI uses the public
[lucy-assistant family CI](https://github.com/apps/lucy-assistant-family-ci) app:
`python scripts/connect_github.py` opens its Install page. A copy under another GitHub
account installs **that same app** on its own repositories; the app private key stays
in the family broker and is never copied into a repository.
[private-repos.md](docs/private-repos.md) explains both.

Work in the [multi-root workspace](LUCY-assistant.code-workspace) or the
[devcontainer](.devcontainer/devcontainer.json).

## The family standard

`python scripts/parity.py` is the mechanical form of this list. A check that cannot
fail is not a check; `tests/test_parity.py` refuses one.

- **Makefile verbs:** `help install fmt lint type imports test cov check run docker clean`.
  `make check` runs exactly `lint type imports test`.
- **Python:** `.python-version` pins 3.11. CI also runs 3.12.
- **Tools:** ruff line-length 100, target `py311`; mypy `strict = true`; coverage
  `fail_under = 100` with branch coverage; pytest `filterwarnings = ["error"]`;
  import-linter contracts; `[dependency-groups] dev` not an extra named `dev`.
- **Config:** an `env_prefix`, `extra="forbid"`, and `check_for_unknown_env_vars` so a
  typo is a startup error.
- **Health:** `/healthy` is liveness — no I/O, and it never fails, because an orchestrator
  restarts a container whose liveness check fails. `/ready` reports each dependency and
  answers 503 when one is unusable. Web-search-api and environments-api also answer their
  older `/health` and `/health/ready` spellings as aliases, kept for the runbooks already
  pointing at them rather than as a second standard.
- **Auth:** consuming services verify tokens with `keyring_client`. Keyring-api itself
  is the issuer, so that check is `n/a` there.
- **Docs:** README, AGENTS.md, CLAUDE.md pointing at AGENTS.md, CONTRIBUTING,
  CHANGELOG (Keep a Changelog), the `docs/` set, `.editorconfig`, `.pre-commit-config.yaml`.
- **Size:** no file under `src/`, `app/`, `tests/`, `scripts/`, or `clients/` may
  exceed 1000 lines. Split the module. [ADR-0005](docs/adr/0005-no-file-over-1000-lines.md).
- **CI identity:** `ci-identity` requires top-level `id-token: write`; app keys and
  long-lived tokens are forbidden in service repositories.
- **Docker secrets:** `docker-secret` requires the BuildKit syntax directive on line 1
  and a `github_token` secret mount on every `uv sync` RUN.

Do not add `pragma: no cover`. 100% means every line is tested.

## The loop

1. Write the test first. Watch it fail for the reason you expect.
2. Write the smallest implementation that passes.
3. `make check` in that service before you commit. Never pipe it to `head`.
4. From this directory, `python scripts/parity.py --repo <name>` if you changed
   something the family cares about (Makefile, pyproject, health routes, config).

Commits are conventional (`feat(scope):`, `fix(scope):`, `chore:`), imperative, no
trailing period. The body explains **why**. Write an ADR in the service — or here, if
the decision is about the family — for anything future-you would otherwise re-litigate.

## What this repository may contain

Bootstrap, parity, compose, the reusable workflow, family docs, ADRs about the family,
and a teaching example under `examples/` (not a ninth service). Not a copier template.
Not copies of the eight services. Not `.env.family`, and not a GitHub token. Run
`make test` for the family tooling and configuration tests.

## Pull requests

Open them against the service you changed. A change to this meta-repo is a separate
PR. [CODEOWNERS](CODEOWNERS) reviews every file here.
