"""`lucy logs`: the hub's log file, filtered to the one conversation you are debugging.

The hub writes one JSON object per line (`LUCY_LOG_FILE`; the family keeps it at
`var/log/lucy.jsonl`), and every line says which session, turn and helper it is about. This
reads that file on the machine it is on -- no hub call, so it works when the hub is the thing
that is broken -- and prints the lines that match, one short line each, or the raw JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import OK, USAGE, CliError
from lucy_api.cli.family import PACKAGE_ROOT, find_family_root

if TYPE_CHECKING:
    import argparse

    from lucy_api.cli.base import Context

FAMILY_LOG = Path("var") / "log" / "lucy.jsonl"
"""Where the family's compose file has the hub write its log, from the family's root."""

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LAST = 200
SHORT_ID = 8
"""How much of an id's tail a line shows. Enough to tell two apart, short enough to read."""

CORRELATION = (("session", "session_id"), ("turn", "turn_id"), ("agent", "agent_id"))
DETAIL = ("operation", "outcome", "error_type")


def add_parser(sub: Any, after: argparse.ArgumentParser) -> None:
    logs = sub.add_parser("logs", parents=[after], help="read the hub's log, filtered")
    logs.add_argument("--file", metavar="PATH", help=f"the log file (default: {FAMILY_LOG})")
    logs.add_argument("--session", metavar="ID", help="only lines about this session")
    logs.add_argument("--turn", metavar="ID", help="only lines about this turn")
    logs.add_argument("--agent", metavar="ID", help="only lines about this helper")
    logs.add_argument(
        "--level", metavar="LEVEL", default="INFO", help="this level and above (default: INFO)"
    )
    logs.add_argument("--grep", metavar="TEXT", help="only lines containing this text")
    logs.add_argument(
        "--last",
        metavar="N",
        type=int,
        default=DEFAULT_LAST,
        help=f"the last N matching lines (default: {DEFAULT_LAST})",
    )
    logs.set_defaults(run=cmd_logs)


def cmd_logs(ctx: Context) -> int:
    args = ctx.args
    level = str(args.level).upper()
    if level not in LEVELS:
        message = f"unknown level {args.level!r}; use one of {', '.join(LEVELS)}"
        raise CliError(message, USAGE)
    if args.last < 1:
        message = "--last must be at least 1"
        raise CliError(message, USAGE)
    path = _log_file(ctx)
    wanted = [line for line in _lines(path) if _matches(line, args, level)][-args.last :]
    for line in wanted:
        ctx.out.write((json.dumps(line) if args.json else _short(line)) + "\n")
    return OK


def _log_file(ctx: Context) -> Path:
    if ctx.args.file:
        path = Path(ctx.args.file).expanduser()
    else:
        root = find_family_root(cwd=Path.cwd(), environ=ctx.environ, origin=PACKAGE_ROOT)
        path = (root or Path.cwd()) / FAMILY_LOG
    if not path.is_file():
        message = f"no log file at {path}"
        hint = (
            "start the family with `make up`, which has the hub write one there, "
            "or pass --file for a hub that writes elsewhere (LUCY_LOG_FILE)"
        )
        raise CliError(message, USAGE, hint=hint)
    return path


def _lines(path: Path) -> list[dict[str, Any]]:
    """Every line of the log that is a JSON object. A torn last line is skipped, not fatal."""
    found: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict):
                found.append(value)
    return found


def _matches(line: dict[str, Any], args: argparse.Namespace, level: str) -> bool:
    if LEVELS.index(str(line.get("level") or "INFO").upper()) < LEVELS.index(level):
        return False
    for flag, field in CORRELATION:
        wanted = getattr(args, flag)
        if wanted and wanted not in str(line.get(field) or ""):
            return False
    return not args.grep or args.grep in json.dumps(line, ensure_ascii=False)


def _short(line: dict[str, Any]) -> str:
    """One line a person reads: time, level, what happened, whose, and how it went."""
    stamp = str(line.get("timestamp") or "")[11:19]
    parts = [stamp, f"{line.get('level', ''):<7}", str(line.get("message") or "")]
    for label, field in CORRELATION:
        value = line.get(field)
        if value:
            parts.append(f"{label}=…{str(value)[-SHORT_ID:]}")
    parts.extend(f"{name}={line[name]}" for name in DETAIL if line.get(name) is not None)
    if line.get("duration_ms") is not None:
        parts.append(f"{line['duration_ms']}ms")
    return " ".join(part for part in parts if part)


__all__ = ["FAMILY_LOG", "add_parser", "cmd_logs"]
