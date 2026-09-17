"""Application logs: one JSON object per line, and a list of things that never appear in one.

Three kinds of record are kept apart on purpose, because collapsing them is how a system
becomes unauditable. The **event stream** is what a client sees and can replay, and it lives
in SQLite for the life of the session. The **audit log** is security-relevant fact -- token
mints, approvals, connections, erasures -- appended to its own table and never trimmed. This
module is the third one: the **application log**, which is operational, goes to stdout, and
is rotated and sampled by whatever is collecting it. It is the only one of the three that a
person outside this system may end up reading, which is why it is the one with a list of
things it may not contain.

## Every line carries the same eleven fields

`request_id`, `trace_id`, `span_id`, `session_id`, `turn_id`, `agent_id`, `parent_agent_id`,
`capability`, `operation`, `duration_ms`, `outcome`. They are present on every line even
when they are null, so a query for `session_id` does not have to also handle the records
where the key is simply missing.

`parent_agent_id` is the field without which a failed twelve-agent run is undebuggable: with
it, one query reconstructs the tree; without it there are twelve unrelated failures and no
way to tell which one caused the others.

The fields are carried in a context variable rather than passed down through signatures,
for the same reason `request_id` is: they are facts about the work in flight, not arguments
to the function doing it, and threading them through would put a logging parameter on code
that has nothing to do with logging. `request_id` itself is read from
:mod:`lucy_api.core.request_id`, which already owns it -- a second copy would be a second
answer.

## What is never logged, and what is logged instead

No token, credential or secret. No memory body. No file contents. No tool result payload. No
message content, unless the person turns on `lucy.log_message_content`, which lives in
settings-api beside user-api's `log_values` and **defaults off**.

Counts, shapes, digests and totals take their place: "tool result, 41,203 tokens, spilled to
$hits", never the result. A field is redacted by *name*, so a caller does not have to
remember which of the things they are logging is sensitive -- and the opt-in reaches only
the person's own message content. A file's contents, a memory body and a tool result stay
out whatever anybody has turned on, because they are somebody's data and not the person's
sentence.

An exception is logged as its **type and its frames**, never its message. A `ValueError`
raised deep in a write path routinely carries the value that caused it, and
:mod:`lucy_api.api.errors` already refuses to put that in a response for the same reason.

The pattern scrubber under all of this is a backstop and not the defence. The defence is
that credential material never reaches a log call in the first place; the scrubber is what
catches the day somebody interpolates a header into a debug message.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from lucy_api.core.request_id import get_request_id

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType


MANDATORY_FIELDS = (
    "request_id",
    "trace_id",
    "span_id",
    "session_id",
    "turn_id",
    "agent_id",
    "parent_agent_id",
    "capability",
    "operation",
    "duration_ms",
    "outcome",
)
"""On every line, null when unknown. A missing key and a null are not the same question."""

REDACTED = "[redacted]"

SECRET_HINTS = (
    "token",
    "secret",
    "credential",
    "password",
    "passphrase",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "cookie",
    "jwt",
    "signature",
)
"""Matched as substrings of a field's name. `refresh_token_expires_at` is caught too, and
losing a timestamp is a cheaper mistake than keeping a refresh token."""

NEVER_FIELDS = frozenset(
    {
        "arguments",
        "body",
        "contents",
        "file_content",
        "file_contents",
        "memory",
        "memory_body",
        "payload",
        "prompt",
        "result",
        "tool_input",
        "tool_result",
        "transcript",
    }
)
"""Somebody's data rather than somebody's sentence. Shaped whatever the opt-in says."""

CONTENT_FIELDS = frozenset({"content", "message_content", "input_text", "output_text"})
"""The person's own words. The only thing `lucy.log_message_content` unlocks."""

SECRET_PATTERNS = (
    # A JWT, which is what every token in this family looks like.
    re.compile(r"\bey[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b[sr]k-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
)
"""Narrow on purpose. A rule broad enough to catch every opaque string would redact the
session ids and digests that make a log worth reading."""

MAX_FRAMES = 20
"""Frames kept from a traceback. The total is reported beside them, never silently lost."""

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)
"""What the logging module puts on a record itself. Everything else came from `extra`."""


@dataclass(frozen=True, slots=True)
class LogContext:
    """The work in flight, as every line about it should describe it.

    `request_id` is absent: it is bound at the edge by :mod:`lucy_api.core.request_id` and
    read from there. Copying it into here would give one id two homes, and the day they
    disagreed would be the day somebody was trying to follow a failure across ten services.
    """

    trace_id: str | None = None
    span_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    capability: str | None = None
    operation: str | None = None


BINDABLE = frozenset(field.name for field in fields(LogContext))

NOTHING_BOUND = LogContext()
"""Read outside a request, a turn or an agent, which is where startup logging happens."""

_context: ContextVar[LogContext | None] = ContextVar("lucy_log_context", default=None)


def current_context() -> LogContext:
    """What the next log line will say about the work in flight."""
    bound = _context.get()
    return bound if bound is not None else NOTHING_BOUND


@contextmanager
def bind(**values: str | None) -> Iterator[LogContext]:
    """Add fields for the duration of the block, keeping whatever was already bound.

    Merging rather than replacing is what makes a sub-agent's log line carry the session it
    belongs to without every caller having to pass it again.
    """
    unknown = sorted(set(values) - BINDABLE)
    if unknown:
        message = (
            f"cannot bind {', '.join(unknown)}; the bindable fields are "
            f"{', '.join(sorted(BINDABLE))}"
        )
        raise ValueError(message)
    merged = LogContext(**{**asdict(current_context()), **values})
    token = _context.set(merged)
    try:
        yield merged
    finally:
        _context.reset(token)


def scrub(text: str) -> str:
    """Replace anything that looks like a credential. The backstop, not the defence."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def shape(value: Any) -> dict[str, Any]:
    """What a value was, without what it said.

    A digest is included for text so that two log lines can be shown to be about the same
    body -- which is the question people actually ask of a redacted field -- without the
    body being recoverable from either of them.
    """
    if isinstance(value, str):
        return {"chars": len(value), "sha256": sha256(value.encode()).hexdigest()[:12]}
    if isinstance(value, dict):
        return {"keys": len(value)}
    if isinstance(value, (list, tuple)):
        return {"items": len(value)}
    return {"type": type(value).__name__}


def _named_like_a_secret(name: str) -> bool:
    return any(hint in name for hint in SECRET_HINTS)


def _frames(
    exc_info: tuple[type[BaseException], BaseException, TracebackType | None],
) -> dict[str, Any]:
    """A traceback as locations only. No message, no arguments, no locals."""
    _, exception, tb = exc_info
    extracted = traceback.extract_tb(tb)
    kept = [f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in extracted[-MAX_FRAMES:]]
    return {"type": type(exception).__name__, "frames": kept, "frames_total": len(extracted)}


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the mandatory fields and none of the forbidden ones.

    `log_message_content` is a parameter rather than a setting read from the environment
    because it is a fact about a *person* -- it lives in settings-api under
    `lucy.log_message_content` -- and the composition root is the only place that knows
    whose process this is.
    """

    def __init__(self, *, log_message_content: bool = False) -> None:
        super().__init__()
        self.log_message_content = log_message_content

    def format(self, record: logging.LogRecord) -> str:
        extras = {name: value for name, value in record.__dict__.items() if name not in _RESERVED}
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        known: dict[str, Any] = dict.fromkeys(MANDATORY_FIELDS)
        known["request_id"] = get_request_id()
        known.update(asdict(current_context()))
        for name in MANDATORY_FIELDS:
            if name in extras:
                known[name] = extras.pop(name)
            line[name] = known[name]
        for name in sorted(extras):
            line[name] = self._value(name, extras[name])
        if record.exc_info is not None and record.exc_info[1] is not None:
            line["error"] = _frames(
                (type(record.exc_info[1]), record.exc_info[1], record.exc_info[2])
            )
        return json.dumps(line, default=str, ensure_ascii=False)

    def _value(self, name: str, value: Any) -> Any:
        lowered = name.lower()
        if _named_like_a_secret(lowered):
            return REDACTED
        if lowered in NEVER_FIELDS:
            return shape(value)
        if lowered in CONTENT_FIELDS and not self.log_message_content:
            return shape(value)
        if isinstance(value, str):
            return scrub(value)
        return value


def configure(
    *,
    level: str = "INFO",
    stream: Any = None,
    log_message_content: bool = False,
) -> logging.Handler:
    """Send structured logs to stdout, replacing any handler this function installed before.

    stdout and not a file: a container's logs belong to whatever is running the container,
    and a service that writes its own files has to be told where, rotate them, and be given
    a disk. Called once, from the composition root.
    """
    root = logging.getLogger()
    for existing in tuple(root.handlers):
        if isinstance(existing.formatter, JsonFormatter):
            root.removeHandler(existing)
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter(log_message_content=log_message_content))
    root.addHandler(handler)
    root.setLevel(level)
    return handler


@contextmanager
def operation(logger: logging.Logger, name: str, **values: str | None) -> Iterator[None]:
    """Time one operation and log how it ended, whichever way it ended.

    `duration_ms` and `outcome` have no other home: a duration measured by the caller is a
    duration somebody forgets to measure on the failure path, which is the one that matters.
    """
    started = time.perf_counter()
    with bind(operation=name, **values):
        try:
            yield
        except BaseException as exc:
            logger.warning(
                "operation",
                extra={
                    "duration_ms": _elapsed(started),
                    "outcome": "error",
                    "error_type": type(exc).__name__,
                },
            )
            raise
        logger.info("operation", extra={"duration_ms": _elapsed(started), "outcome": "ok"})


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


__all__ = [
    "BINDABLE",
    "CONTENT_FIELDS",
    "MANDATORY_FIELDS",
    "NEVER_FIELDS",
    "NOTHING_BOUND",
    "REDACTED",
    "SECRET_HINTS",
    "JsonFormatter",
    "LogContext",
    "bind",
    "configure",
    "current_context",
    "operation",
    "scrub",
    "shape",
]
