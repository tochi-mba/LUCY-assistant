"""Reclamation as a projection over the transcript, never a rewrite of it.

The window fills. Something has to give. The rungs below are cheapest-first: references
and truncation already happened before this module is asked, then old tool results, then
thinking, then a compact, then a split. Each rung that drops something names the count,
because a model that cannot tell it is reasoning from a fragment will invent the missing
middle.

Nothing here writes. The caller decides whether a compact flag becomes a compaction row,
and ``GET /v1/sessions/{id}/context`` can run the same filter without mutating anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.context.projection import Item

PROTECTED_PREFIXES = ("notes.",)
"""Tool results that compaction and reclamation must not drop.

The memory index is already a summary. Throwing away the one expansion the model just
fetched is how an assistant forgets the person mid-sentence.
"""


@dataclass(frozen=True, slots=True)
class Limits:
    """The knobs one reclaim pass reads, already clamped by TurnPolicy."""

    window: int = 200_000
    warn_at_percent: int = 60
    compact_at_percent: int = 72
    tool_results_kept: int = 3


@dataclass(frozen=True, slots=True)
class Reclaimed:
    """The items still in the window, and the confession of what left."""

    items: tuple[Item, ...]
    notices: tuple[str, ...]
    should_compact: bool = False
    tools_cleared: int = 0
    thinking_cleared: int = 0


def reclaim(
    items: Sequence[Item],
    *,
    used: int,
    limits: Limits,
) -> Reclaimed:
    """Drop what the window can afford to lose, in order, naming every drop.

    The percent is computed from the caller's last assemble, not re-estimated here: items
    do not carry a token count, and inventing one would disagree with the assembler.
    """
    window = limits.window
    percent = int(100 * used / window) if window else 0
    notices: list[str] = []
    if percent >= limits.warn_at_percent and window:
        notices.append(f"context is {percent}% of {window:,} tokens")
    if percent < limits.compact_at_percent:
        return Reclaimed(items=tuple(items), notices=tuple(notices))

    kept, tools_cleared = _drop_old_tools(tuple(items), limits.tool_results_kept)
    if tools_cleared:
        droppable = tools_cleared + _kept_unprotected(kept, limits.tool_results_kept)
        notices.append(
            f"cleared {tools_cleared} of {droppable} tool results; "
            f"kept the last {limits.tool_results_kept}"
        )
    kept, thinking_cleared = _drop_thinking(kept)
    if thinking_cleared:
        notices.append(f"cleared {thinking_cleared} thinking items")
    return Reclaimed(
        items=kept,
        notices=tuple(notices),
        should_compact=True,
        tools_cleared=tools_cleared,
        thinking_cleared=thinking_cleared,
    )


def _drop_old_tools(items: tuple[Item, ...], keep: int) -> tuple[tuple[Item, ...], int]:
    droppable = [
        index
        for index, item in enumerate(items)
        if item.kind == "tool_result" and not _protected(item)
    ]
    doomed = set(droppable) if keep <= 0 else set(droppable[:-keep])
    if not doomed:
        return items, 0
    return tuple(item for index, item in enumerate(items) if index not in doomed), len(doomed)


def _kept_unprotected(items: tuple[Item, ...], keep: int) -> int:
    remaining = sum(1 for item in items if item.kind == "tool_result" and not _protected(item))
    return remaining if keep <= 0 else min(keep, remaining)


def _drop_thinking(items: tuple[Item, ...]) -> tuple[tuple[Item, ...], int]:
    thinking = {index for index, item in enumerate(items) if item.kind in {"thinking", "reasoning"}}
    if not thinking:
        return items, 0
    return tuple(item for index, item in enumerate(items) if index not in thinking), len(thinking)


def _protected(item: Item) -> bool:
    operation = _operation(item)
    return any(operation.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def _operation(item: Item) -> str:
    try:
        payload = json.loads(item.body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("operation") or payload.get("op") or ""
    return value if isinstance(value, str) else ""


__all__ = ["PROTECTED_PREFIXES", "Limits", "Reclaimed", "reclaim"]
