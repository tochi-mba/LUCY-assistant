"""How a transcript item reads to the model: as what happened, not as the row it is stored in.

The transcript keeps every item as the structure its writer produced, and clients read those
rows. The model used to be handed the same rows, as JSON in the person's voice: an approval as
an opaque id, `is_automatic: false`, and one sentence twice, as `description` and as `reason`;
every tool result with `duration_ms: 183.080810546875`, an empty `note` and an empty `notices`.
None of it was wrong, and none of it helped the model read what had happened.

So each kind gets one plain rendering, in brackets where it is the harness speaking. Anything
without a rendering here is still passed as JSON, so nothing a writer adds is ever lost.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def readable(kind: str, content: object) -> str:
    """The item as the model reads it: its rendering when it has the shape one expects."""
    if isinstance(content, str):
        return content
    known = _RENDERERS.get(kind)
    if known is None or not isinstance(content, dict) or not known[0] <= content.keys():
        return json.dumps(content, ensure_ascii=False)
    return known[1](content)


def _tool_result(content: dict[str, Any]) -> str:
    step = content.get("step_id")
    operation = content.get("operation")
    lines = [f"[step {step}: {operation} -- {content.get('status')}]"]
    if content.get("note"):
        lines.append(f"for: {content['note']}")
    lines.extend(f"notice: {notice}" for notice in content.get("notices") or ())
    if content.get("error"):
        lines.append(f"error: {content['error']}")
    if content.get("summary"):
        lines.append(str(content["summary"]))
    return "\n".join(lines)


def _approval_request(content: dict[str, Any]) -> str:
    arguments = content.get("arguments")
    shown = ", ".join(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in (arguments.items() if isinstance(arguments, dict) else ())
    )
    call = f"{content.get('tool')}({shown})"
    return f"[asked the person to approve {call}: {content.get('description')}]"


def _approval_response(content: dict[str, Any]) -> str:
    if content.get("approved"):
        lifetime = str(content.get("lifetime") or "once")
        span = "this once" if lifetime == "once" else f"for this {lifetime}"
        return f"[the person approved it, {span}]"
    instruction = str(content.get("instruction") or "").strip()
    return f"[the person declined it: {instruction}]" if instruction else "[the person declined it]"


def _error(content: dict[str, Any]) -> str:
    return f"[error {content.get('code')}: {content.get('detail')}]"


_RENDERERS: dict[str, tuple[frozenset[str], Callable[[dict[str, Any]], str]]] = {
    "tool_result": (frozenset({"step_id", "operation", "status"}), _tool_result),
    "approval_request": (frozenset({"tool", "description"}), _approval_request),
    "approval_response": (frozenset({"approved"}), _approval_response),
    "error": (frozenset({"code", "detail"}), _error),
}
"""Each kind's rendering, and the keys an item needs to have for it to apply."""


__all__ = ["readable"]
