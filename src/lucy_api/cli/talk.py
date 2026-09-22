"""`lucy talk`: send a message, print the reply, leave the turn running if you hang up.

The hub's one write path is `POST /v1/sessions/{id}/inputs`. This command is a client of
that path plus the event stream, not a second conversation loop. Closing the CLI must not
cancel the turn — that is the whole point of making the turn durable before it runs.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import TYPE_CHECKING, Any

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
    from collections.abc import Iterator

    from lucy_api.cli.base import Context

ACCEPTED = 202
CREATED = 201
READ_SECONDS = 120.0
TEXT_DELTA = "lucy.content.text.delta"
TURN_COMPLETED = "lucy.turn.completed"
TURN_FAILED = "lucy.turn.failed"
STREAM_DONE = "lucy.stream.done"


def cmd_talk(ctx: Context) -> int:
    """One message, or several if somebody is sitting at a prompt."""
    if not ctx.token:
        message = "not signed in"
        raise CliError(message, REFUSED, hint="run `lucy setup`")
    words = tuple(getattr(ctx.args, "words", ()) or ())
    session_id = str(getattr(ctx.args, "session", "") or "")
    if words:
        _send(ctx, session_id, " ".join(words).strip())
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
        current = _send(ctx, current, text)
    return OK


def _send(ctx: Context, session_id: str, text: str) -> str:
    if not text:
        message = "say something, or pipe a message on stdin"
        raise CliError(message, USAGE)
    import httpx  # noqa: PLC0415 - kept out of `lucy --help`

    timeout = httpx.Timeout(TIMEOUT_SECONDS, read=READ_SECONDS)
    try:
        with httpx.Client(timeout=timeout) as client:
            current = session_id or _create_session(client, ctx)
            turn_id, reply = _ask(client, ctx, current, text)
    except httpx.HTTPError as exc:
        message = f"cannot reach Lucy at {ctx.url}"
        hint = (
            f"start it with `lucy serve`, or set {URL_VAR} to where it runs "
            f"({exc.__class__.__name__})"
        )
        raise CliError(message, UNREACHABLE, hint=hint) from exc
    payload = {"session_id": current, "turn_id": turn_id, "text": reply}
    ctx.say(f"session {current}")
    ctx.emit(payload, reply)
    return current


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


def _ask(client: Any, ctx: Context, session_id: str, text: str) -> tuple[str, str]:
    posted = client.post(
        f"{ctx.url}/v1/sessions/{session_id}/inputs",
        headers={**headers(ctx.token), "Idempotency-Key": str(uuid.uuid4())},
        json={"events": [{"type": "input.message", "content": text}]},
    )
    body = _body(posted)
    if posted.status_code != ACCEPTED or not body.get("id"):
        raise CliError(_problem(body, "Lucy refused that message"), REFUSED)
    turn_id = str(body["id"])
    return turn_id, _read_reply(client, ctx, session_id, turn_id)


def _read_reply(client: Any, ctx: Context, session_id: str, turn_id: str) -> str:
    parts: list[str] = []
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
            elif event_type == TURN_FAILED and envelope.get("turn_id") == turn_id:
                message = "Lucy could not finish that turn"
                raise CliError(message, REFUSED)
            elif event_type == STREAM_DONE or (
                event_type == TURN_COMPLETED and envelope.get("turn_id") == turn_id
            ):
                break
    return "".join(parts)


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
