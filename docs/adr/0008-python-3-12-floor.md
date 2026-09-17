# ADR-0008: the family runs on Python 3.12

**Status:** accepted

## Context

Every repository pinned `.python-version` to 3.11, targeted `py311` in ruff, ran mypy at
3.11 and built on a `python:3.11-slim` base — while CI already ran the test matrix on 3.12
everywhere. So 3.12 was tested but never trusted.

The hub being built in this repository depends on `weftai`, which declares
`requires-python >= 3.12` and uses PEP 695 type parameters (`class RunContext[Ctx]`) in its
public API. That syntax is a parse error on 3.11 and cannot be backported. A hub on 3.12
beside eight services on 3.11 would mean two Pythons in one family, a parity check that has
to learn an exception, and a `.python-version` a developer can no longer trust.

## Decision

The floor for every repository in the family is **Python 3.12**, and CI's matrix is
**3.12 and 3.13**.

Concretely, in each of the eight services, the example service, and this repository:
`.python-version` holds `3.12`; `requires-python` is `>=3.12`; ruff's `target-version` is
`py312`; mypy's `python_version` is `3.12`; the Dockerfile base is `python:3.12-slim*` (or
`UV_PYTHON=3.12` where the base image is somebody else's); `.pre-commit-config.yaml` uses
`python3.12`; and the classifiers say 3.12 and 3.13. `scripts/parity.py` enforces it through
`PINNED_PYTHON` and `RUFF_TARGET`, whose values now also render the check descriptions, so
the constant and the sentence a developer reads cannot drift apart again.

## Why

**One Python, or the pin means nothing.** `.python-version` exists so that everyone,
everywhere, resolves the same interpreter. A per-repository exception turns it into a hint.

**3.12 was already the tested floor.** The matrix has run it since the reusable workflow
existed; this change is the admission of what was already true, plus the Dockerfiles.

**The lint rules that come with it are the point, not the tax.** Raising ruff's
`target-version` enabled `UP046`/`UP047`/`UP040`, which found nine places across the family
still declaring generics with `TypeVar` and `TypeAlias`. Converting them to PEP 695 removed
four module-level `T = TypeVar("T")` declarations and made each file one style rather than
two. The rules did not create work; they surfaced work that was already owed.

## What it costs

Anybody with a 3.11 virtualenv re-creates it — `uv sync` does that on its own, and
`scripts/bootstrap.sh` now installs 3.12 and 3.13. Every `uv.lock` was regenerated, which is
one large diff per repository, once. Deployments that pinned a `python:3.11` base image pull
a new one. Nothing about the wire, the storage, or the tokens changes.

## What would change our minds

A dependency the family needs that has no 3.12 wheel. There is none today, and the one
dependency that forced this decision goes the other way: `weftai` requires 3.12 or newer.
