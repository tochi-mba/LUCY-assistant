"""`lucy talk`: send a message, print the reply, leave the turn running if you hang up.

The hub's one write path is `POST /v1/sessions/{id}/inputs`. This command is a client of
that path plus the event stream, not a second conversation loop. Closing the CLI must not
cancel the turn — that is the whole point of making the turn durable before it runs.

A line that starts with `/` and names a command is the person talking to the client, not to
Lucy: `/context` says how full the window is, `/compact` and `/uncompact` act on it, `/new`
starts a fresh conversation, `/help` lists them. `//` sends a line that starts with `/`.
Anything else starting with `/` -- a path, say -- goes to Lucy as written.
"""

from __future__ import annotations

import io
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lucy_api.cli import window as windows
from lucy_api.cli.base import (
    HTTP_OK,
    OK,
    REFUSED,
    TIMEOUT_SECONDS,
    UNREACHABLE,
    URL_VAR,
    USAGE,
    CliError,
    headers,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from lucy_api.cli.base import Context

ACCEPTED = 202
CREATED = 201
READ_SECONDS = 120.0
TEXT_DELTA = "lucy.content.text.delta"
TURN_COMPLETED = "lucy.turn.completed"
TURN_FAILED = "lucy.turn.failed"
ITEM_ADDED = "lucy.content.item.added"
STREAM_DONE = "lucy.stream.done"
CONTEXT_STATUS = "lucy.context.status"
COMPACTION_APPLIED = "lucy.compaction.applied"

COMMAND = re.compile(r"/[a-z][a-z-]*")
"""What a command looks like. `/etc/hosts is broken` does not, so it is sent to Lucy."""

HELP = """\
/context          how full this conversation's window is
/compact [N]      summarise the older turns now, keeping the newest N (default: your setting)
/uncompact [ID]   undo a compaction (default: the one Lucy is reading)
/new              start a fresh conversation
/session          which conversation this is
/help             this list
/quit             leave (Ctrl-D does too); a turn still running carries on
//text            send a message that starts with /"""


@dataclass
class Reply:
    """What one turn sent back: the words, and what the client noticed on the way."""

    turn_id: str
    text: str
    context: dict[str, Any] | None = None
    compacted: list[dict[str, Any]] = field(default_factory=list)


def cmd_talk(ctx: Context) -> int:
    """One message, or several if somebody is sitting at a prompt."""
    if not ctx.token:
        message = "not signed in"
        raise CliError(message, REFUSED, hint="run `lucy setup`")
    words = tuple(getattr(ctx.args, "words", ()) or ())
    session_id = str(getattr(ctx.args, "session", "") or "")
    if words:
        line = " ".join(words).strip()
        if _command(line):
            _run_command(ctx, session_id, line, latest=True)
            return OK
        _send(ctx, session_id, _unescape(line))
        return OK
    if ctx.interactive:
        return _repl(ctx, session_id)
    incoming = ctx.in_.read() if ctx.in_ is not None else ""
    _send(ctx, session_id, incoming.strip())
    return OK


def _repl(ctx: Context, session_id: str) -> int:
    current = session_id
    reader = ctx.in_ if ctx.in_ is not None else io.StringIO()
    while True:
        if not ctx.args.quiet and not ctx.args.json:
            print("you: ", file=ctx.err, end="", flush=True)
        line = reader.readline()
        if line == "":
            break
        text = line.strip()
        if not text:
            continue
        if _command(text):
            try:
                current, leave = _run_command(ctx, current, text, latest=False)
            except CliError as exc:
                # A command that fails is said and forgotten; the conversation goes on.
                ctx.say(f"lucy: {exc}" + (f" ({exc.hint})" if exc.hint else ""))
                continue
            if leave:
                break
            continue
        current = _send(ctx, current, _unescape(text))
    return OK


def _command(line: str) -> str:
    """The command a line names, or "" when the line is a message."""
    first = line.split(maxsplit=1)[0] if line else ""
    return first if COMMAND.fullmatch(first) else ""


def _unescape(line: str) -> str:
    return line[1:] if line.startswith("//") else line


def _run_command(ctx: Context, current: str, line: str, *, latest: bool) -> tuple[str, bool]:
    """Do what a `/` line asks. Returns the conversation now current, and whether to leave.

    `latest` is for a one-shot `lucy talk /context`, where there is no conversation in hand
    and the person's most recent one is the sensible default; at a prompt, the conversation
    is whichever this prompt has been talking in.
    """
    name, _, rest = line.partition(" ")
    argument = rest.strip()
    if name in {"/quit", "/exit"}:
        return current, True
    if name == "/help":
        ctx.emit({"commands": HELP.splitlines()}, HELP)
        return current, False
    if name == "/new":
        ctx.say("a fresh conversation starts with your next message")
        return "", False
    if name == "/session":
        ctx.emit({"session_id": current or None}, current or "no conversation yet")
        return current, False
    handler = _ACTIONS.get(name)
    if handler is None:
        message = f"{name} is not a command; /help lists them, and // sends it to Lucy"
        raise CliError(message, USAGE)
    with windows.open_client() as client:
        session_id = current
        if not session_id:
            if not latest:
                message = "no conversation yet: say something first"
                raise CliError(message, REFUSED)
            session_id = windows.resolve_session(client, ctx)
        payload, text = handler(client, ctx, session_id, argument)
    ctx.emit({"session_id": session_id, **payload}, text)
    return session_id, False


def _context(
    client: Any, ctx: Context, session_id: str, _argument: str
) -> tuple[dict[str, Any], str]:
    report = windows.window(client, ctx, session_id)
    return report, windows.describe(report)


def _compact(
    client: Any, ctx: Context, session_id: str, argument: str
) -> tuple[dict[str, Any], str]:
    keep: int | None = None
    if argument:
        if not argument.isdigit():
            message = "/compact takes how many recent turns to keep, as a whole number"
            raise CliError(message, USAGE)
        keep = int(argument)
    done = windows.compact(client, ctx, session_id, keep)
    return done, windows.describe_compaction(done)


def _uncompact(
    client: Any, ctx: Context, session_id: str, argument: str
) -> tuple[dict[str, Any], str]:
    undone = windows.uncompact(client, ctx, session_id, argument or None)
    return undone, windows.describe_undo(undone)


_ACTIONS: dict[str, Callable[[Any, Context, str, str], tuple[dict[str, Any], str]]] = {
    "/context": _context,
    "/compact": _compact,
    "/uncompact": _uncompact,
}


def _send(ctx: Context, session_id: str, text: str) -> str:
    if not text:
        message = "say something, or pipe a message on stdin"
        raise CliError(message, USAGE)
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    timeout = httpx.Timeout(TIMEOUT_SECONDS, read=READ_SECONDS)
    try:
        with httpx.Client(timeout=timeout) as client:
            current = session_id or _create_session(client, ctx)
            reply = _ask(client, ctx, current, text)
    except httpx.HTTPError as exc:
        message = f"cannot reach Lucy at {ctx.url}"
        hint = (
            f"start it with `lucy serve`, or set {URL_VAR} to where it runs "
            f"({exc.__class__.__name__})"
        )
        raise CliError(message, UNREACHABLE, hint=hint) from exc
    payload = {
        "session_id": current,
        "turn_id": reply.turn_id,
        "text": reply.text,
        "context": reply.context,
    }
    ctx.say(f"session {current}")
    ctx.emit(payload, reply.text)
    for compaction in reply.compacted:
        ctx.say(ctx.style.dim(_compacted(compaction)))
    if reply.context is not None:
        ctx.say(ctx.style.dim(windows.status_line(reply.context)))
    return current


def _compacted(event: dict[str, Any]) -> str:
    turns = event.get("turns")
    covered = f"turns 1-{turns}" if isinstance(turns, int) and turns > 1 else "the oldest turn"
    return f"compacted {covered} into a summary to stay within the window (/uncompact undoes it)"


def _create_session(client: Any, ctx: Context) -> str:
    response = client.post(
        f"{ctx.url}/v1/sessions",
        headers={**headers(ctx.token), "Idempotency-Key": str(uuid.uuid4())},
        json={},
    )
    body = _body(response)
    if response.status_code != CREATED or not body.get("id"):
        raise CliError(_problem(body, "could not start a conversation"), REFUSED)
    return str(body["id"])


def _ask(client: Any, ctx: Context, session_id: str, text: str) -> Reply:
    posted = client.post(
        f"{ctx.url}/v1/sessions/{session_id}/inputs",
        headers={**headers(ctx.token), "Idempotency-Key": str(uuid.uuid4())},
        json={"events": [{"type": "input.message", "content": text}]},
    )
    body = _body(posted)
    if posted.status_code != ACCEPTED or not body.get("id"):
        raise CliError(_problem(body, "Lucy refused that message"), REFUSED)
    turn_id = str(body["id"])
    return _read_reply(client, ctx, session_id, turn_id)


def _read_reply(client: Any, ctx: Context, session_id: str, turn_id: str) -> Reply:
    parts: list[str] = []
    failure = ""
    reply = Reply(turn_id=turn_id, text="")
    with client.stream(
        "GET",
        f"{ctx.url}/v1/sessions/{session_id}/events",
        headers=headers(ctx.token),
    ) as response:
        if response.status_code != HTTP_OK:
            refused = "the event stream was refused"
            raise CliError(refused, REFUSED)
        for event_type, envelope in _frames(response.iter_lines()):
            data = envelope.get("data")
            payload = data if isinstance(data, dict) else {}
            if event_type == TEXT_DELTA:
                delta = payload.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
            elif event_type == CONTEXT_STATUS and envelope.get("turn_id") == turn_id:
                reply.context = payload
            elif event_type == COMPACTION_APPLIED and payload.get("trigger") == "auto":
                reply.compacted.append(payload)
            elif event_type == ITEM_ADDED and envelope.get("turn_id") == turn_id:
                failure = _error_detail(payload) or failure
            elif event_type == TURN_FAILED and envelope.get("turn_id") == turn_id:
                message = "Lucy could not finish that turn"
                raise CliError(f"{message}: {failure}" if failure else message, REFUSED)
            elif event_type == STREAM_DONE or (
                event_type == TURN_COMPLETED and envelope.get("turn_id") == turn_id
            ):
                break
    reply.text = "".join(parts)
    return reply


def _error_detail(item: dict[str, Any]) -> str:
    """Why a turn failed, from the error item the hub writes before it says so.

    The failure event says only that the turn failed; the sentence saying why arrives just
    before it, as the turn's error item. Without it a person saw "Lucy could not finish that
    turn" for a usage limit, a missing credential and a model that stopped mid-answer alike.
    """
    content = item.get("content")
    if item.get("type") != "error" or not isinstance(content, dict):
        return ""
    detail = content.get("detail")
    return detail if isinstance(detail, str) else ""


def _frames(lines: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    event_type = ""
    data = ""
    for raw in lines:
        line = raw.decode() if isinstance(raw, bytes) else str(raw)
        line = line.rstrip("\n")
        if line == "":
            if event_type and data:
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    yield event_type, parsed
            event_type, data = "", ""
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()


def _body(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _problem(body: dict[str, Any], fallback: str) -> str:
    detail = body.get("detail")
    return detail if isinstance(detail, str) and detail else fallback
