# ADR-0002: shared clients live in the hub

**Status:** accepted

## Context

Every consuming service has to verify a keyring JWT the same way, and several will
resolve a person's settings the same way. The cheap thing is a `lucy-clients`
package, or a copy-paste of fifty lines into each repository.

## Decision

The client lives **in the repository that owns the protocol**.

- `keyring_client` is Keyring-api/`clients/python`.
- `settings_client` is Settings-api/`clients/python`.

Consuming services use tagged git sources with `subdirectory = "clients/python"`.
Git fetches them using the developer's account credentials, or CI/image build's
read-only family token. They do not vendor a fork, and this meta-repo does not host
a third copy. [ADR-0006](0006-the-family-may-be-private.md) records authentication.

## Why

**The protocol and the client must change together.** A new claim, a new error body,
a new cache rule is one PR in the hub, with the tests that pin the behaviour. A
shared package sitting in a ninth repository would lag, and a vendored copy would
silently diverge — which, for token verification, is a vulnerability rather than a
style issue.

**The hub is the test double's home.** Keyring's own fake and settings-api's
`FakeSettingsClient` ship next to the real client so a consumer's tests exercise
the same refusals production will.

## What it costs

`make install` needs access to the hub's pinned git tag; no sibling checkout is needed.
A breaking client change is still two PRs, in a defined order: hub first, consumers
second. A private copy must preserve those tags and refresh consumer lockfiles after
retargeting source URLs.

## What would change our minds

A published, versioned package on the index, with the hub as its source, is the
intended end state — not a different home. Moving the source into this meta-repo
would put the protocol next to people who do not run it.
