# Changelog

All notable changes to the LUCY hub and the family desk are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **The hub.** `src/lucy_api/` is the beginning of Lucy: configuration that refuses an
  unknown `LUCY_*` variable at startup, a keyring token verifier with the family's
  401-versus-503 split, `GET /healthy` and `GET /ready`, and `GET /v1/me`. It is held to
  the same gates as every sibling, and `python scripts/parity.py` now scores this
  repository too. See [ADR-0009](docs/adr/0009-the-hub-lives-here.md).

### Changed

- **Breaking:** the family floor is **Python 3.12**, and CI runs 3.12 and 3.13.
  [ADR-0008](docs/adr/0008-python-3-12-floor.md) records why: `weftai`, which the hub
  depends on, requires 3.12 and uses PEP 695 type parameters that do not parse on 3.11.
  `scripts/parity.py` enforces the new floor, and its check descriptions are now rendered
  from the same constants it checks against, so the two cannot drift apart.
- `pytest.ini` was folded into `pyproject.toml`, which the repository now has because it
  ships a package.
