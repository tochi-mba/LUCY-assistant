"""Where a spilled tool result starts, when the model points at a unique snippet of it.

A cap that always shows the head and the tail is the right default: errors cluster at the
end of logs and diffs. It is the wrong default the moment the model already knows which
middle it needs. Re-running the same step with a textual fingerprint is how it says so.
Display starts at the only match; if that window is still too large, the same head-and-tail
spill applies to the suffix.

The needle is sought in the scrubbed body, never in the framing wrapper. The wrapper is
ours, and a model copying a provenance line out of it would "find" every result. A miss or
a collision shows nothing of the payload: dumping the body on a bad fingerprint would be
the cap by another name. Notices name line numbers, not the text that failed to match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lucy_api.context.tokens import CHARS_PER_TOKEN, Estimate

RESULT_TOKEN_CAP = 25_000
"""The most one tool result may contribute to the context."""

QUOTE_LIMIT = 48
"""How much of a fingerprint a notice may quote. The rest is an ellipsis."""

MAX_OCCURRENCES = 8
"""How many collision sites a notice lists before it stops enumerating."""

NEEDLE_KEYS = ("show_from", "fingerprint")
"""Plan-step fields, then input fields, in that order. ``show_from`` is the one we teach."""

UNSHOWN = (
    "This result was not shown again because the fingerprint was missing or not unique. "
    "The notices name the lines to distinguish."
)


@dataclass(frozen=True, slots=True)
class View:
    """The body the model is allowed to see, plus the sentences that explain the cut."""

    text: str
    notices: tuple[str, ...] = ()
    shown: bool = True


def needle_from(raw: Any) -> str:
    """The fingerprint on a plan step or an executed step, or empty if there is none.

    Step-level fields win over ``input``, so a model can point at a window without putting
    a Lucy-only key through an operation schema that would reject it.
    """
    if not isinstance(raw, dict):
        return ""
    found = _first_needle(raw)
    if found:
        return found
    incoming = raw.get("input")
    if isinstance(incoming, dict):
        return _first_needle(incoming)
    return ""


def attach_needles(plan: Any, result: Any) -> None:
    """Copy each plan step's fingerprint onto the executed step of the same id.

    weftai's result objects do not have to round-trip unknown plan fields. The loop still
    has the plan the model wrote, so the window can start where it asked even when the
    executor never heard of ``show_from``.
    """
    if not isinstance(plan, dict) or not isinstance(result, dict):
        return
    wanted: dict[str, str] = {}
    for step in _steps_of(plan):
        if not isinstance(step, dict):
            continue
        needle = needle_from(step)
        step_id = str(step.get("id") or "")
        if needle and step_id:
            wanted[step_id] = needle
    for raw in _steps_of(result):
        if not isinstance(raw, dict) or needle_from(raw):
            continue
        step_id = str(raw.get("id") or "")
        if step_id in wanted:
            raw["show_from"] = wanted[step_id]


def without_needles(plan: Any) -> Any:
    """A copy weftai can execute: Lucy's window fields are not operation arguments.

    The original plan is left intact so :func:`attach_needles` can still find the
    fingerprint after the runtime has run.
    """
    if not isinstance(plan, dict):
        return plan
    steps = [
        _without_needle_keys(step) if isinstance(step, dict) else step for step in _steps_of(plan)
    ]
    return {**plan, "steps": steps}


def focus(text: str, needle: str) -> View:
    """Slice from a unique match, or refuse to dump the body when the match is not unique."""
    fingerprint = needle.strip()
    if not fingerprint:
        return View(text)
    starts = _starts(text, fingerprint)
    quoted = _quote(fingerprint)
    if not starts:
        return View(
            UNSHOWN,
            notices=(f"the fingerprint {quoted} was not found in this result",),
            shown=False,
        )
    if len(starts) > 1:
        return View(
            UNSHOWN,
            notices=(
                f"the fingerprint {quoted} matched {len(starts)} times ({_sites(text, starts)}). "
                "Pick a longer unique snippet",
            ),
            shown=False,
        )
    start = starts[0]
    line, column = _position(text, start)
    notice = f"showing from the unique fingerprint {quoted} at line {line} character {column}"
    return View(text[start:], notices=(notice,))


def spill(text: str, *, cap: int = RESULT_TOKEN_CAP) -> tuple[str, tuple[str, ...]]:
    """Keep a result inside the token cap without silently deleting either end of it."""
    tokens = Estimate().count(text)
    if tokens <= cap:
        return text, ()
    budget_chars = max(cap, 1) * CHARS_PER_TOKEN
    head = max(budget_chars // 2, 1)
    tail = max(budget_chars - head, 1)
    kept = f"{text[:head]}\n…\n{text[-tail:]}"
    shown = Estimate().count(kept)
    notice = f"showing {shown} of {tokens} tokens; the rest spilled"
    return kept, (notice,)


def window(text: str, needle: str, *, cap: int = RESULT_TOKEN_CAP) -> View:
    """Focus, then spill the focused body if it still overruns the cap."""
    focused = focus(text, needle)
    if not focused.shown:
        return focused
    kept, extra = spill(focused.text, cap=cap)
    return View(kept, notices=focused.notices + extra)


def allow_show_from(schema: dict[str, Any]) -> dict[str, Any]:
    """Teach the plan schema the field the model uses to pick a window.

    weftai's compiled schema does not know about Lucy's spill, and a model that is never
    told a field exists will not invent it reliably. Patching the step object is cheaper
    than forking the compiler, and it stays true if weftai adds properties of its own.
    """
    patched = _copy(schema)
    _patch_steps(patched)
    return patched


def _steps_of(payload: dict[str, Any]) -> tuple[Any, ...]:
    steps = payload.get("steps")
    if isinstance(steps, list | tuple):
        return tuple(steps)
    return ()


def _without_needle_keys(step: dict[str, Any]) -> dict[str, Any]:
    cleaned = {key: value for key, value in step.items() if key not in NEEDLE_KEYS}
    incoming = cleaned.get("input")
    if isinstance(incoming, dict):
        cleaned["input"] = {key: value for key, value in incoming.items() if key not in NEEDLE_KEYS}
    return cleaned


def _first_needle(mapping: dict[str, Any]) -> str:
    for key in NEEDLE_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _starts(text: str, needle: str) -> tuple[int, ...]:
    found: list[int] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return tuple(found)
        found.append(index)
        start = index + 1


def _sites(text: str, starts: tuple[int, ...]) -> str:
    shown = starts[:MAX_OCCURRENCES]
    parts = [
        f"line {line} character {column}" for line, column in (_position(text, i) for i in shown)
    ]
    extra = len(starts) - len(shown)
    if extra:
        parts.append(f"and {extra} more")
    return ", ".join(parts)


def _position(text: str, index: int) -> tuple[int, int]:
    line = text.count("\n", 0, index) + 1
    column = index - (text.rfind("\n", 0, index) + 1) + 1
    return line, column


def _quote(needle: str) -> str:
    compact = " ".join(needle.split())
    if len(compact) > QUOTE_LIMIT:
        compact = compact[: QUOTE_LIMIT - 1] + "…"
    return json.dumps(compact, ensure_ascii=False)


def _copy(schema: dict[str, Any]) -> dict[str, Any]:
    duplicate: dict[str, Any] = json.loads(json.dumps(schema))
    return duplicate


def _patch_steps(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _patch_steps(item)
        return
    if not isinstance(node, dict):
        return
    props = node.get("properties")
    if isinstance(props, dict) and "id" in props and "op" in props and "show_from" not in props:
        props["show_from"] = {
            "type": "string",
            "description": (
                "A unique snippet of a spilled result to start showing from. Display starts "
                "at the only match; if that window is still too large, its head and tail are kept."
            ),
        }
    for value in node.values():
        _patch_steps(value)


__all__ = [
    "NEEDLE_KEYS",
    "RESULT_TOKEN_CAP",
    "UNSHOWN",
    "View",
    "allow_show_from",
    "attach_needles",
    "focus",
    "needle_from",
    "spill",
    "window",
    "without_needles",
]
