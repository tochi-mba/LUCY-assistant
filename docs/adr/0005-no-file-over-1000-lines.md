# ADR-0005: no file over 1000 lines

**Status:** accepted

## Context

A module that has grown past a thousand lines is a module nobody can hold in
their head, review in one sitting, or test without skimming. The family already
requires 100% branch coverage and strict types; those gates do not catch "this
file is now three files wearing a trench coat."

## Decision

No Python file under a service's source package, `tests/`, `scripts/`, or
`clients/` may exceed **1000** physical lines. The family checker
(`max-file-lines` in `scripts/parity.py`) fails the repository when one does.
Split the module.

The same cap applies to files created in this meta-repo.

## Why

**Review is the control.** A 1,400-line `config.py` hides a validator. A
1,400-line test file hides the case that was never written. Coverage 100% of a
file you cannot read is a false calm.

**The split is the design.** Forcing a cut usually reveals a type, a store, or
a router that wanted its own name.

## What it costs

Occasional extra files and imports. A generated snapshot might need to live
outside the scanned directories if a catalogue dump genuinely cannot split —
we have not met one yet. Counting physical lines (not statements) means a
short file of blank lines can still trip the gate; that is accepted.

## What would change our minds

A generated file whose contents *are* the product (an OpenAPI dump, a catalogue
page) could be excluded by path if splitting it would lie. We would document
the exclusion in the checker, not raise the cap. Raising the cap because a
file is "almost done growing" is how the cap dies.
