# ADR-0001: a meta-repo, not a monorepo

**Status:** accepted

## Context

Eight services already exist as eight GitHub repositories. The cheap thing would have
been to fold them into one tree so a single `git clone` got everything, a single CI
graph tested everything, and a single PR could land a cross-cutting change.

## Decision

This repository is a **family desk**. It holds bootstrap, the parity checker, compose,
the reusable workflow, and the documents that apply to all eight. Each service remains
its own git repository, listed in `repos.txt` and cloned *beside* this tree. The eight
directories are gitignored here and are never committed.

## Why

**Release pressure is not shared.** A credential-vault patch and a Playwright recipe
do not belong in one version number or one review.

**Blast radius.** A checkout of media-tool should not contain keyring's signing-key
code, and a checkout of keyring should not contain a browser. Separate repositories
make that the default rather than a subtree discipline we would forget.

**Work in flight.** Other people are editing the eight repositories at the same time.
A monorepo would serialize that through one default branch. A meta-repo can clone and
then **leave an existing checkout alone** — no pull, no reset — which is the only
safe behaviour while those clones are dirty.

## What it costs

A second clone step (`scripts/bootstrap.sh`), a parity checker because the standard
cannot be a file that lives in only one tree, and no atomic cross-repo PR. Cross-cutting
work is two pull requests and a documented order.

## What would change our minds

A single deployable artifact that *is* the assistant, with a single process boundary,
would make eight repositories theatre. We are not building that. A copier template
that stamped out the next service was considered and is **out of scope**: the eight
already exist, and their drift is a checker, not a generator.
