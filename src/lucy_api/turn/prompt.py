"""Assemble the prompt the model is shown from a session's own rows.

The context engine is a library. This is the adapter that reads items out of the store and
hands them to it, so the supervisor and ``GET /v1/sessions/{id}/context`` cannot drift
apart: both ask the same function, and a bug in one is a bug in both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from lucy_api.context.assembler import Window, assemble
from lucy_api.context.build import Built, build_context
from lucy_api.context.build import Turn as ContextTurn
from lucy_api.context.ladder import Limits, Reclaimed, reclaim
from lucy_api.context.projection import Compaction, Item
from lucy_api.context.sources import StateRequest
from lucy_api.context.state import SECTION_ID as LIVE_SECTION_ID
from lucy_api.context.tokens import default_counter
from lucy_api.context.types import Band, Budget, BudgetSnapshot, SessionSnapshot, shares_for
from lucy_api.model.types import Message, Role
from lucy_api.prompt.sections import PromptContext, prompt_version, render_all
from lucy_api.turn.readable import readable

if TYPE_CHECKING:
    from lucy_api.context.build import Live
    from lucy_api.context.types import Assembled


@dataclass(frozen=True, slots=True)
class SessionView:
    """Everything one prompt needs from storage, already read."""

    session_id: str
    items: list[dict[str, Any]]
    capabilities: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    advertised: tuple[str, ...] = ()
    session: dict[str, Any] | None = None
    compactions: list[dict[str, Any]] | None = None
    turn_number: int = 1
    live: Live | None = None
    response_style: str = "natural"
    window: int = 200_000
    reserve_percent: int = 13
    warn_at_percent: int = 60
    compact_at_percent: int = 72
    tool_results_kept: int = 3
    schema_tokens: int = 0
    """The plan schema's size, sent with every request. Only whoever built the turn knows it."""


class ViewLimits(TypedDict):
    """The SessionView fields TurnPolicy owns. A TypedDict so splat stays typed."""

    window: int
    reserve_percent: int
    warn_at_percent: int
    compact_at_percent: int
    tool_results_kept: int


def conversation_order(
    items: list[dict[str, Any]], turns: list[dict[str, Any]], visible_turns: set[str]
) -> list[dict[str, Any]]:
    """Put completed turns before later input that was queued while they ran.

    The append-only log records a second person's message the moment it arrives, which can
    precede the first turn's eventual assistant reply. The model must instead see each
    completed turn as a coherent exchange, then the input it is answering.

    A rollback hides the superseded turn's items. Interrupt keeps the progress already made.
    """
    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["turn_id"], []).append(item)
    ordered = list(grouped.pop(None, ()))
    turn_order = sorted(
        turns,
        key=lambda turn: (
            min(
                (int(item["seq"]) for item in grouped.get(str(turn["id"]), ())),
                default=2**63 - 1,
            ),
            float(turn.get("created_at", 0.0)),
            str(turn["id"]),
        ),
    )
    for turn in turn_order:
        turn_id = str(turn["id"])
        if str(turn.get("stop_reason") or "") == "rollback":
            grouped.pop(turn_id, ())
            continue
        if turn_id in visible_turns:
            ordered.extend(grouped.pop(turn_id, ()))
    return ordered


def view_limits(policy: Any) -> ViewLimits:
    """The SessionView fields TurnPolicy owns, so HTTP preview and a live turn cannot drift."""
    return {
        "window": int(getattr(policy, "max_context_tokens", 200_000)),
        "reserve_percent": int(getattr(policy, "reserve_percent", 13)),
        "warn_at_percent": int(getattr(policy, "warn_at_percent", 60)),
        "compact_at_percent": int(getattr(policy, "compaction_trigger_percent", 72)),
        "tool_results_kept": int(getattr(policy, "tool_results_kept", 3)),
    }


def projected_rows(view: SessionView) -> tuple[list[dict[str, Any]], Reclaimed]:
    """Drop reclaimable items from the view without touching the transcript."""
    converted = items_from_rows(view.items)
    used = _tokens_for(converted) + _carried(view)
    result: Reclaimed = reclaim(
        converted,
        used=used,
        limits=Limits(
            window=view.window,
            warn_at_percent=view.warn_at_percent,
            compact_at_percent=view.compact_at_percent,
            tool_results_kept=view.tool_results_kept,
        ),
    )
    kept = {item.id for item in result.items}
    rows = [row for row in view.items if str(row["id"]) in kept]
    return rows, result


def schema_tokens(schema: object) -> int:
    """A plan schema's size in tokens, as it goes over the wire."""
    return default_counter().count(json.dumps(schema, separators=(",", ":")))


def _carried(view: SessionView) -> int:
    """What every request carries besides the transcript: the fixed prompt and the plan schema.

    Counting the transcript alone told the model "11 of 200,000 tokens (0% used)" on a request
    that carried some 17,000 -- and warnings and compaction read the same number, so with a
    small window the prompt could fill it while the line still said nearly nothing was used.
    """
    fixed = render_all(_prompt_context(view))
    return sum(section.tokens for section in fixed) + view.schema_tokens


def _prompt_context(view: SessionView) -> PromptContext:
    return PromptContext(
        capabilities=view.capabilities,
        deferred=view.deferred,
        advertised=view.advertised,
        response_style=view.response_style,
    )


def _tokens_for(items: tuple[Item, ...]) -> int:
    counter = default_counter()
    return sum(counter.count(item.body) for item in items)


def items_from_rows(rows: list[dict[str, Any]]) -> tuple[Item, ...]:
    """Transcript rows as the projection wants them: a body, a role, a turn boundary."""
    converted: list[Item] = []
    for order, row in enumerate(rows):
        text = readable(str(row.get("type") or "message"), row.get("content"))
        converted.append(
            Item(
                id=str(row["id"]),
                seq=int(row["seq"]),
                role=str(row["role"]),
                body=text,
                turn_id=row.get("turn_id"),
                kind=str(row.get("type") or "message"),
                order=order,
            )
        )
    return tuple(converted)


def messages_from_items(items: tuple[Item, ...]) -> tuple[Message, ...]:
    """The conversational turns, skipping anything that is not a message."""
    out: list[Message] = []
    for item in items:
        if item.kind != "message":
            continue
        role = Role.assistant if item.role == "assistant" else Role.user
        out.append(Message(role, item.body))
    return tuple(out)


def assembled_prompt(*, capabilities: tuple[str, ...] = ()) -> Assembled:
    """The stable prefix, with no history. What ``GET /v1/prompt/preview`` returns."""
    prompt = PromptContext(capabilities=capabilities)
    return assemble(Window(prompt=prompt))


def preview_document(*, capabilities: tuple[str, ...] = ()) -> dict[str, Any]:
    assembled = assembled_prompt(capabilities=capabilities)
    return document_from(assembled, version=prompt_version())


def document_from(assembled: Assembled, *, version: str) -> dict[str, Any]:
    return {
        "prompt": assembled.text(),
        "version": version,
        "total": assembled.total,
        "bands": {band.value: count for band, count in assembled.by_band.items()},
        "notices": list(assembled.notices),
        "sections": [
            {
                "id": section.id,
                "band": section.band.value,
                "tokens": section.tokens,
                "truncated": section.truncated,
            }
            for section in assembled.sections
        ],
    }


async def context_for_session(view: SessionView) -> dict[str, Any]:
    """The exact prompt a turn would send, plus the per-band counts."""
    built = await _build(view)
    return document_from(built.context, version=prompt_version())


async def system_and_messages(
    view: SessionView, *, notice: str = ""
) -> tuple[str, tuple[Message, ...]]:
    """What the loop's ``assemble`` callable returns: a system prompt and the turns.

    The same assembled sections power ``GET /v1/sessions/{id}/context``. This adapter then
    maps those sections onto provider trust channels: authored instructions become system
    text and all claims, history, results, and live state become data messages.
    """
    built = await _build(view)
    system, messages = _model_prompt(view, built)
    if notice:
        system = f"{system}\n\n{notice}"
    return system, messages


def _model_prompt(view: SessionView, built: Built) -> tuple[str, tuple[Message, ...]]:
    """Split the priced document at the model's actual trust boundaries.

    Only authored instructions use the provider's system channel. Standing claims, tool
    results, conversation history, and live state remain data messages. The live block is
    inserted immediately before a newly submitted user message; after a tool round it stays
    last so the next model call sees the state resulting from that work.
    """
    items = {f"history.item.{item.id}": item for item in items_from_rows(view.items)}
    current_input = next(
        (
            f"history.item.{row['id']}"
            for row in reversed(view.items)
            if str(row.get("type") or "message") == "message" and row.get("role") == "user"
        ),
        "",
    )
    system_parts: list[str] = []
    messages: list[Message] = []
    live: Message | None = None
    current_input_index: int | None = None
    for section in built.context.sections:
        if section.band is Band.system:
            system_parts.append(section.body)
            continue
        if section.id == LIVE_SECTION_ID:
            live = Message(Role.user, section.body)
            continue
        item = items.get(section.id)
        if item is None:
            messages.append(Message(Role.user, section.body))
            continue
        role = Role.assistant if item.role == "assistant" and item.kind == "message" else Role.user
        prefix = f"{item.role}: "
        content = section.body.removeprefix(prefix)
        messages.append(Message(role, content))
        if section.id == current_input:
            current_input_index = len(messages) - 1
    if live is not None:
        last = len(messages) - 1
        if current_input_index is not None and current_input_index == last:
            messages.insert(current_input_index, live)
        else:
            messages.append(live)
    return "\n\n".join(system_parts), tuple(messages)


async def _build(view: SessionView) -> Built:
    rows, reclaimed = projected_rows(view)
    row = view.session or {}
    allowance = Budget(window=view.window, shares=shares_for(view.reserve_percent))
    built = await build_context(
        StateRequest(
            now=datetime.now(UTC),
            session=SessionSnapshot(
                id=view.session_id,
                profile=str(row.get("profile", "personal")),
                title=str(row.get("title", "")),
                turn_number=view.turn_number,
                permission_mode=str(row.get("permission_mode", "ask")),
                incognito=bool(row.get("incognito", 0)),
            ),
            budget=BudgetSnapshot(
                used=reclaimed.used,
                window=view.window,
                reclaimable=reclaimed.reclaimable,
            ),
        ),
        ContextTurn(
            items=items_from_rows(rows),
            prompt=_prompt_context(view),
            compactions=compactions_from_rows(view.compactions or ()),
        ),
        live_from=view.live,
        budget=allowance,
    )
    if not reclaimed.notices:
        return built
    context = replace(built.context, notices=(*built.context.notices, *reclaimed.notices))
    return replace(built, context=context)


def compactions_from_rows(
    rows: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> tuple[Compaction, ...]:
    """Store rows as the projection wants them. Inactive rows stay out."""
    return tuple(
        Compaction(
            seq=int(row["seq"]),
            summary=str(row.get("summary") or ""),
            covers_from=int(row["covers_from"]),
            covers_to=int(row["covers_to"]),
            active=bool(row.get("active", 1)),
        )
        for row in rows
    )


__all__ = [
    "SessionView",
    "assembled_prompt",
    "context_for_session",
    "conversation_order",
    "document_from",
    "items_from_rows",
    "messages_from_items",
    "preview_document",
    "projected_rows",
    "schema_tokens",
    "system_and_messages",
    "view_limits",
]
