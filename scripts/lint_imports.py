#!/usr/bin/env python3
"""Run the import-linter contracts without going through its console script.

Usage::

    python scripts/lint_imports.py          # every contract in pyproject.toml
    python scripts/lint_imports.py --debug  # import-linter's own debug output

``import-linter`` ships only a ``lint-imports`` console script: it has no ``__main__``,
so ``python -m importlinter`` cannot work. On a Windows host with an Application Control
policy, the generated ``.exe`` shim in ``.venv/Scripts`` is refused before it starts
(``Failed to spawn: lint-imports``), which takes ``make imports`` -- and therefore
``make check`` -- down with it. ``ruff`` and ``uvicorn`` are allowed on the same box and
``mypy`` and ``pytest`` are not, so this is about which shims a policy happens to trust
rather than anything the tools do differently.

Calling the use case directly is immune to that, needs no policy exemption, and behaves
identically everywhere else, so the Makefile uses this on every platform rather than
branching on one.

``configuration.configure()`` is not optional and not a detail: it registers the option
readers that parse ``[tool.importlinter]``, and ``importlinter.cli`` runs it at import
time. Calling the use case without it fails with a bare ``'USER_OPTION_READERS'`` and no
contracts checked -- which still prints a banner, so it reads like a pass at a glance.

The use case returns ``True`` when every contract is kept; this translates that into the
shell's convention rather than passing a bool to :func:`sys.exit`, where ``True`` would
mean exit status 1.

Exit status: 0 when every contract is kept, 1 when any is broken.
"""

from __future__ import annotations

import sys

from importlinter import configuration
from importlinter.application.use_cases import lint_imports

configuration.configure()


def main(argv: list[str] | None = None) -> int:
    """Lint the contracts declared in ``pyproject.toml``."""
    args = sys.argv[1:] if argv is None else argv
    kept = lint_imports(config_filename=None, is_debug_mode="--debug" in args, verbose=False)
    return 0 if kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
