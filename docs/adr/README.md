# Architecture decision records

One file per decision that future-us would otherwise re-litigate. Each says what was
decided, what it cost, and what would make us change our minds.

These are **family** decisions. A service's own ADRs stay in that service.

| ADR | Decision |
| --- | --- |
| [0001](0001-meta-repo-not-monorepo.md) | A meta-repo that clones eight git repositories, not a monorepo |
| [0002](0002-shared-clients-live-in-the-hub.md) | `keyring_client` and `settings_client` live in the hub that owns the protocol |
| [0003](0003-bearer-canonical.md) | `Authorization: Bearer` is the canonical identity header |
| [0004](0004-port-assignments.md) | Family ports 8001–8008 |
| [0005](0005-no-file-over-1000-lines.md) | No source, test, script, or client file over 1000 lines |
| [0006](0006-the-family-may-be-private.md) | Any repository may be private; one developer sign-in, and a shared read-only GitHub App with an OIDC token broker for CI |
| [0007](0007-public-base-hubs.md) | Canonical published repositories are public; shared clients fetch anonymously |
| [0008](0008-python-3-12-floor.md) | The family runs on Python 3.12; CI's matrix is 3.12 and 3.13 |
| [0009](0009-the-hub-lives-here.md) | The hub is `src/lucy_api/` in this repository, not a tenth sibling |
| [0010](0010-ports-8000-and-8009.md) | Lucy on 8000, Memory-api reserved on 8009 |
