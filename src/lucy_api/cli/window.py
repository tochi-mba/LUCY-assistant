"""`lucy context`, `lucy compact`, `lucy uncompact`: the window, seen and acted on by hand.

Automatic compaction runs on its own when a conversation fills its window. These commands
are the person's side of the same thing: how full is it, compact it now, put it back. Each
is a thin client of one hub route, so what they print is the figure the hub acts on, and
`lucy talk`'s `/context`, `/compact` and `/uncompact` call the same functions.

Without `--session`, they act on your most recent conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.cli.base import (
    HTTP_OK,
    OK,
    REFUSED,
    TIMEOUT_SECONDS,
    CliError,
    headers,
    unreachable,
)

if TYPE_CHECKING:
    import argparse

    from lucy_api.cli.base import Context

NOT_FOUND = 404
MAX_KEEP = 100
"""The hub's ceiling on `keep_recent_turns`; checked here so the mistake is named locally."""

ABOUT = (
    "Automatic compaction runs on its own when a conversation fills its window; these act "
    "on the same thing by hand. Without --session, your most recent conversation."
)

EXAMPLES = """\
examples:
  lucy context                   how full your latest conversation's window is
  lucy context -s ses_123        the same, for one conversation
  lucy compact                   summarise the older turns now
  lucy compact --keep 1          keep only the newest turn verbatim
  lucy uncompact                 undo the compaction the model is reading
"""


def add_parser(sub: Any, after: argparse.ArgumentParser) -> None:
    """`context`, `compact` and `uncompact`, each with `--session`."""
    import argparse  # noqa: PLC0415 - only for the formatter class

    def command(name: str, summary: str) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = sub.add_parser(
            name,
            parents=[after],
            help=summary,
            description=f"{summary.capitalize()}.\n\n{ABOUT}",
            epilog=EXAMPLES,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        parser.add_argument(
            "-s", "--session", metavar="ID", help="which conversation (default: your latest)"
        )
        return parser

    context = command("context", "how full a conversation's window is")
    context.set_defaults(run=cmd_context)

    compact = command("compact", "summarise a conversation's older turns now")
    compact.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help="how many of the newest turns stay verbatim (default: your setting)",
    )
    compact.set_defaults(run=cmd_compact)

    uncompact = command("uncompact", "undo a compaction")
    uncompact.add_argument(
        "id", nargs="?", help="which compaction (default: the one the model is reading)"
    )
    uncompact.set_defaults(run=cmd_uncompact)


def cmd_context(ctx: Context) -> int:
    with open_client() as client:
        session_id = resolve_session(client, ctx)
        report = window(client, ctx, session_id)
    ctx.say(f"session {session_id}")
    ctx.emit({"session_id": session_id, **report}, describe(report))
    return OK


def cmd_compact(ctx: Context) -> int:
    keep = getattr(ctx.args, "keep", None)
    with open_client() as client:
        session_id = resolve_session(client, ctx)
        done = compact(client, ctx, session_id, keep)
    ctx.say(f"session {session_id}")
    ctx.emit({"session_id": session_id, **done}, describe_compaction(done))
    return OK


def cmd_uncompact(ctx: Context) -> int:
    chosen = getattr(ctx.args, "id", None)
    with open_client() as client:
        session_id = resolve_session(client, ctx)
        undone = uncompact(client, ctx, session_id, chosen)
    ctx.say(f"session {session_id}")
    ctx.emit({"session_id": session_id, **undone}, describe_undo(undone))
    return OK


# --------------------------------------------------------------------------------------
# The calls, shared with `lucy talk`'s chat commands
# --------------------------------------------------------------------------------------


def window(client: Any, ctx: Context, session_id: str) -> dict[str, Any]:
    """`GET /context/window`: how full it is, in the figure the hub acts on."""
    return _call(client, ctx, "GET", f"/v1/sessions/{session_id}/context/window")


def compact(client: Any, ctx: Context, session_id: str, keep: int | None) -> dict[str, Any]:
    """`POST /compact`, keeping `keep` turns verbatim, or the person's setting when `None`."""
    if keep is not None and not 0 <= keep <= MAX_KEEP:
        message = f"--keep must be between 0 and {MAX_KEEP} turns"
        raise CliError(message, REFUSED)
    body = {} if keep is None else {"keep_recent_turns": keep}
    return _call(client, ctx, "POST", f"/v1/sessions/{session_id}/compact", body)


def uncompact(
    client: Any, ctx: Context, session_id: str, compaction_id: str | None
) -> dict[str, Any]:
    """`POST /uncompact`, on the named compaction or the one the model is reading."""
    if not compaction_id:
        listed = _call(client, ctx, "GET", f"/v1/sessions/{session_id}/compactions")
        rows = listed.get("data")
        shown = (
            [row for row in rows if isinstance(row, dict) and row.get("shown")]
            if isinstance(rows, list)
            else []
        )
        if not shown:
            message = "nothing to undo: the model is not reading any compaction"
            raise CliError(message, REFUSED)
        compaction_id = str(shown[0]["id"])
    body = {"id": compaction_id}
    return _call(client, ctx, "POST", f"/v1/sessions/{session_id}/uncompact", body)


# --------------------------------------------------------------------------------------
# What a person reads
# --------------------------------------------------------------------------------------


def describe(report: dict[str, Any]) -> str:
    """The window, in two or three sentences a person can act on."""
    used, size, percent = (
        _number(report, "used_tokens"),
        _number(report, "window_tokens"),
        _number(report, "percent"),
    )
    lines = [f"{percent}% of the window used ({used:,} of {size:,} tokens)."]
    state = report.get("state")
    at = _number(report, "compact_at_percent")
    automatic = report.get("automatic_compaction", True) is not False
    if state == "over":
        lines.append(
            "This turn's prompt alone is bigger than the window: raise max_context_tokens."
        )
    elif not automatic:
        lines.append(
            "Automatic compaction is off after three failures; `lucy compact` still tries."
        )
    elif state == "compacting":
        lines.append(f"Past {at}%: the next turn compacts automatically.")
    else:
        left = _number(report, "tokens_until_compaction")
        lines.append(f"Compacts automatically at {at}%, {left:,} tokens from now.")
    summarised = _number(report, "summarised_turns")
    if summarised:
        turns = "turn 1 is" if summarised == 1 else f"turns 1-{summarised} are"
        lines.append(f"{turns.capitalize()} read as a summary.")
    return " ".join(lines)


def status_line(report: dict[str, Any]) -> str:
    """One short line for the bottom of a reply: `context 42% · compacts at 72%`."""
    percent, at = _number(report, "percent"), _number(report, "compact_at_percent")
    tail = "over the window" if report.get("state") == "over" else f"compacts at {at}%"
    return f"context {percent}% · {tail}"


def describe_compaction(done: dict[str, Any]) -> str:
    turns = _number(done, "turns")
    covered = "turn 1" if turns == 1 else f"turns 1-{turns}"
    keep = done.get("keep_recent_turns")
    kept = f"; the newest {keep} stay word for word" if isinstance(keep, int) else ""
    before, after = done.get("context_before"), done.get("context_after")
    change = ""
    if isinstance(before, dict) and isinstance(after, dict):
        change = f" Window: {_number(before, 'percent')}% -> {_number(after, 'percent')}%."
    return f"Compacted {covered} into a summary{kept}.{change} `lucy uncompact` puts them back."


def describe_undo(undone: dict[str, Any]) -> str:
    return (
        f"Undid {undone.get('id')}: entries {undone.get('covers_from')}-"
        f"{undone.get('covers_to')} are read word for word again."
    )


# --------------------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------------------


def open_client() -> Any:
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    return httpx.Client(timeout=TIMEOUT_SECONDS)


def resolve_session(client: Any, ctx: Context) -> str:
    """The named conversation, or the most recent one."""
    chosen = str(getattr(ctx.args, "session", "") or "")
    if chosen:
        return chosen
    listed = _call(client, ctx, "GET", "/v1/sessions?limit=1&order=desc")
    rows = listed.get("data")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and rows[0].get("id"):
        return str(rows[0]["id"])
    message = "you have no conversations yet"
    raise CliError(message, REFUSED, hint="start one with `lucy talk`")


def _call(
    client: Any, ctx: Context, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One request. Unreachable, refused and not-found are three different sentences."""
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    if not ctx.token:
        message = "not signed in"
        raise CliError(message, REFUSED, hint="run `lucy setup`")
    try:
        response = client.request(method, f"{ctx.url}{path}", headers=headers(ctx.token), json=body)
    except httpx.HTTPError as exc:
        raise unreachable(ctx.url, exc) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if response.status_code == NOT_FOUND:
        message = "no such conversation or compaction on this account"
        raise CliError(message, REFUSED, hint="`lucy context` without -s uses your latest")
    if response.status_code != HTTP_OK:
        detail = payload.get("detail")
        message = detail if isinstance(detail, str) and detail else "Lucy refused that"
        raise CliError(message, REFUSED)
    return payload


def _number(source: dict[str, Any], key: str) -> int:
    value = source.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = [
    "add_parser",
    "cmd_compact",
    "cmd_context",
    "cmd_uncompact",
    "compact",
    "describe",
    "describe_compaction",
    "describe_undo",
    "open_client",
    "resolve_session",
    "status_line",
    "uncompact",
    "window",
]
