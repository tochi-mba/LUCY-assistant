# ADR-0016: the `repos` capability, Github-api on 8011, and grants limited to one repository

**Status:** accepted (2026-10-01). Amends [ADR-0010](0010-ports-8000-and-8009.md).

## Context

Lucy could work inside a sandbox but could not see or touch GitHub. People asked for what a
coding assistant does there -- read and open pull requests, watch CI, merge, create and
delete repositories -- with two kinds of control: how much GitHub lets Lucy see, and which
of those actions Lucy may take without asking.

## Decision

**The model-facing name is `repos`; GitHub is the provider behind it.** The capability, its
operations, permissions, prompt page, skill and settings group say "repositories". The word
GitHub appears only where the person meets the provider they connected: the onboarding card
title ("GitHub repositories", as "Spotify music"), the probe's detail ("GitHub as @octo"),
and the setup steps. A second provider changes none of the model's vocabulary. The service
name `github-api` is denylisted wherever service names are (`PUBLIC_SERVICE_NAMES`).

**A new public sibling, Github-api, on port 8011.** It holds no credential of its own: each
request resolves the caller's from keyring (`service="github"`), so access is per account
and per profile by construction. Env prefix `GHAPI_`, not `GITHUB_API_`: Actions runners
export `GITHUB_API_URL`, which the family's unknown-variable check would refuse. 8010 is
recorded here as the optional Laya service's number (until now only in
`docs/adding-a-service.md` and `scripts/laya_server.py`); 8011 is Github-api's. The family is
nine services.

**Two layers of control, never merged.** What GitHub lets Lucy do is chosen on GitHub: the
Lucy GitHub App's installation (all repositories or selected ones) or a fine-grained token.
What Lucy may do without asking is the permission gate's, with the answers every capability
has. Seven permissions split the writes by consequence (`comment`, `change`, `merge`, `ci`,
`create`, `destroy`, `watch`); `destroy` is `destructive`, so the default approval policy
asks about it even in `auto`.

**A grant may be limited to values of its permission's `tally` field** ("always, for this
repository"). The rule is general -- any permission with a `tally` can be limited -- and
`repos` is its first use. Three rules:

1. **The gate matches the whole value from the step's literal input**, never the card's
   label (which is cut at 40 characters). So every gated `repos` write takes `repo` as plain
   text, not a `$reference`; otherwise the gate would see nothing to match before the plan
   runs, and the limit could never apply.
2. **Limits join.** A second limited allow for the same permission and profile adds its
   values; an unlimited allow clears the limit; a deny replaces it and cannot itself be
   limited (a deny for one repository is not a thing the gate can promise).
3. **A narrow grant does not hide a wide one.** A profile grant limited to `a/one` overlays an
   account-wide unlimited allow; a call on `a/two` falls through to the account-wide grant
   rather than stopping at the narrow one and asking.

**Watching uses [jobs and signals](0015-jobs-and-signals.md).** `repos.watch` opens a durable
subscription at Github-api, which signals when CI settles or a pull request merges; with
`wake`, the person's yes records standing consent, and the woken turn acts under it.

## Consequences

- Github-api joins `repos.txt`, `repos.lock`, compose and the workspace in the change that
  publishes it, so bootstrap never clones a repository that is not there; until then the
  capability probes `unavailable`.
- The Lucy GitHub App must expire user tokens; keyring cannot renew a token without a
  refresh token ([docs/repos.md](../repos.md)).
- `permission_grants` gains `only_json`; existing databases gain the column at startup.
