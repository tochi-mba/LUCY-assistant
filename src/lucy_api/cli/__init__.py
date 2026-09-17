"""The `lucy` command.

Installed globally (`uv tool install`), this is how a person talks to Lucy from any
directory without knowing where the repository is or which port the hub listens on.

It is a **client**, not the server. `lucy serve` runs the hub in the foreground for
development; everything else talks to one over HTTP. That split matters: the command a
person types every day should work the same whether the hub is on this laptop, in compose,
or on a machine down the hall, and the only thing that changes is `LUCY_URL`.

Output is plain text on a terminal. Every command takes `--json` for a machine, because a
CLI that can only be read by a human is a CLI that cannot be scripted.
"""

from __future__ import annotations

from lucy_api.cli.main import main

__all__ = ["main"]
