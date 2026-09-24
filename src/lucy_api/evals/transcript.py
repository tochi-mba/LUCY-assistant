"""What happened in one turn, read off the session's transcript.

The transcript is the hub's own record, which is what makes it evidence: every item carries
the ``turn_id`` it belongs to, a tool result carries the operation, its status and the
summary the *model* was shown, and an approval is an item rather than a side channel. So
"did ``workspace.write`` run in this turn" is answered from what the hub wrote down, never
from what the model said it did.

Items a helper agent wrote carry no turn id, so they are not attributed to any turn: the
checks see the parent conversation, and a scenario that needs to prove an effect a helper
had uses ``verify``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MESSAGE = "message"
TOOL_RESULT = "tool_result"
APPROVAL_REQUEST = "approval_request"
APPROVAL_RESPONSE = "approval_response"
ERROR = "error"
ASSISTANT = "assistant"
USER = "user"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One executed operation, as the transcript recorded it."""

    operation: str
    status: str
    summary: str = ""
    error: str = ""
    note: str = ""

    @property
    def text(self) -> str:
        """Everything a result says, for a ``results`` expectation to search."""
        return "\n".join(part for part in (self.summary, self.error) if part)


@dataclass(frozen=True, slots=True)
class Ask:
    """One approval the turn parked on, and how it was answered, if it was."""

    approval_id: str
    operation: str
    permission: str = ""
    arguments: str = ""
    answer: str = ""
    """``approved`` or ``denied`` once answered; empty while it is still waiting."""


@dataclass(frozen=True, slots=True)
class Exchange:
    """One turn: what was said, what Lucy replied, what ran and what it asked."""

    said: str
    reply: str
    results: tuple[ToolResult, ...] = ()
    asks: tuple[Ask, ...] = ()
    errors: tuple[str, ...] = ()


def exchange_for(items: list[dict[str, Any]], turn_id: str) -> Exchange:
    """Everything the transcript attributes to ``turn_id``, in the order it was written."""
    mine = [item for item in items if item.get("turn_id") == turn_id]
    answers = _answers(mine)
    said: list[str] = []
    replies: list[str] = []
    results: list[ToolResult] = []
    asks: list[Ask] = []
    errors: list[str] = []
    for item in mine:
        kind = item.get("type")
        content = item.get("content")
        if kind == MESSAGE:
            (replies if item.get("role") == ASSISTANT else said).append(_words(content))
        elif kind == TOOL_RESULT:
            results.append(_result(_object(content)))
        elif kind == APPROVAL_REQUEST:
            asks.append(_ask(_object(content), answers))
        elif kind == ERROR:
            errors.append(_error(content))
    return Exchange(
        said="\n\n".join(text for text in said if text),
        reply="\n\n".join(text for text in replies if text),
        results=tuple(results),
        asks=tuple(asks),
        errors=tuple(errors),
    )


def pending_approvals(items: list[dict[str, Any]], turn_id: str) -> tuple[str, ...]:
    """The approvals this turn asked for that nobody has answered yet, oldest first."""
    return tuple(ask.approval_id for ask in exchange_for(items, turn_id).asks if not ask.answer)


def _answers(items: list[dict[str, Any]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in items:
        if item.get("type") != APPROVAL_RESPONSE:
            continue
        content = _object(item.get("content"))
        found[str(content.get("approval_id") or "")] = (
            "approved" if content.get("approved") is True else "denied"
        )
    return found


def _result(content: dict[str, Any]) -> ToolResult:
    return ToolResult(
        operation=str(content.get("operation") or ""),
        status=str(content.get("status") or ""),
        summary=str(content.get("summary") or ""),
        error=str(content.get("error") or ""),
        note=str(content.get("note") or ""),
    )


def _ask(content: dict[str, Any], answers: dict[str, str]) -> Ask:
    approval_id = str(content.get("approval_id") or "")
    return Ask(
        approval_id=approval_id,
        operation=str(content.get("tool") or content.get("permission") or ""),
        permission=str(content.get("permission") or ""),
        arguments=_plain(_object(content.get("arguments"))),
        answer=answers.get(approval_id, ""),
    )


def _plain(arguments: dict[str, Any]) -> str:
    """What the ask would do, as ``key=value`` for its plain values -- the part a person reads."""
    return ", ".join(
        f"{key}={value}"
        for key, value in arguments.items()
        if isinstance(value, str | int | float) and not isinstance(value, bool)
    )


def _error(content: object) -> str:
    body = _object(content)
    if body:
        code = str(body.get("code") or "error")
        detail = str(body.get("detail") or "")
        return f"{code}: {detail}" if detail else code
    return _words(content)


def _words(content: object) -> str:
    """A message's text. Anything that is not text is not something a person read."""
    if isinstance(content, str):
        return content
    body = _object(content)
    text = body.get("text")
    return text if isinstance(text, str) else ""


def _object(content: object) -> dict[str, Any]:
    return content if isinstance(content, dict) else {}


__all__ = ["Ask", "Exchange", "ToolResult", "exchange_for", "pending_approvals"]
