# ADR-0011: a private service extends the family, it is never named by it

**Status:** accepted

## Context

The family is public. Some integrations are not, and more will not be: a tool somebody
builds for themselves, a service wrapping a licence they cannot redistribute, or an
integration with something under NDA. Public repositories had accumulated names and
implementation details for an operator-local integration in prose, settings, tests and
deployment configuration.

So a private service is not private. Its name, its capabilities, its retention policy and its
quality settings are published, and anybody who clones the family learns that it exists and
roughly what it does. That is a leak whether or not anybody minds today, and the fix gets
harder with every repository that adds a reference.

It is also a scaling problem rather than a one-off tidy-up. The second private service would
need the same eight edits in the same eight public files, and the fiftieth mention would be
found by somebody grepping for the forty-ninth.

## Decision

**A public repository never names a private service.** Private services attach through
declared extension points, and the public side knows only that extension points exist.

Four seams, all of which the family already half has:

**1. Settings namespaces are discovered, not listed.** `domain/catalogue/__init__.py`
assembles the built-in modules *plus* anything registered under the entry-point group
`settings_api.namespaces`. A private service ships a small package that registers its own
namespace module. The public catalogue has no module for it and no import of one.

**2. Prompt feeds are discovered the same way.** The hub's table of fields a sibling may put
in front of the model gains an entry-point group, `lucy.feeds`. A private capability brings
its own fields and its own toggles with it.

**3. Capability packs are already an extension point.** `lucy.capabilities` was designed for
third parties and works unchanged for private ones.

**4. Compose is an overlay.** The public `docker-compose.yml` describes the public family.
A private service lives in `docker-compose.local.yml`, which is gitignored and merged by
`make up`. That is the same mechanism as `.repos.local.txt`, applied to the thing that
actually starts the process.

**And a check that keeps it true.** `scripts/parity.py` gains a check that reads the private
repository names from `.repos.local.txt` and fails if any public repository's source, tests
or documentation contains one. Nobody has to remember the rule; the build remembers it.

## Why an entry point rather than a configuration file

A namespace is code: it has defaults, bounds, validation and prose. A configuration file
would mean either shipping a schema language for settings definitions, or a private service
handing arbitrary Python to a public process at runtime. An entry point is the mechanism
Python already has for "this installed package extends that installed package", it is
discovered at import rather than at request time, and it fails at startup when the extension
is malformed.

It also means the public process has no knowledge of what might attach. There is no list to
leak and no name to grep for.

## What it costs

A private service becomes a small package rather than a directory, with an
`entry-points` table and a version. That is a real cost and it is the point: the boundary is
now something somebody has to cross deliberately.

`make up` grows an overlay argument. The parity check grows a pass over the tree, which is
cheap and catches the failure that matters — somebody writing a helpful example.

The existing prose mentions are replaced with a generic illustration rather than a different
real service, because picking another real service is how this happens again.

## What would change our minds

If the family ever had no private members, the seams would be unused indirection. That is
not the direction of travel: the mechanism is cheaper and safer than auditing prose
forever.

If a private service needed to change public *behaviour* rather than extend it, an extension
point would not be enough. None does, and one that did would be an argument for it being
public.
