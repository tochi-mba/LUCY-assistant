"""Assemble the prompt the model is shown from a session's own rows.

The context engine is a library. This is the adapter that reads items out of the store and
hands them to it, so the supervisor and ``GET /v1/sessions/{id}/context`` cannot drift
apart: both ask the same function, and a bug in one is a bug in both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lucy_api.context.assembler import Window, assemble
from lucy_api.context.build import Built, build_context
from lucy_api.context.build import Turn as ContextTurn
from lucy_api.context.projection import Compaction, Item
from lucy_api.context.sources import StateRequest
from lucy_api.context.types import BudgetSnapshot, SessionSnapshot
from lucy_api.model.types import Message, Role
from lucy_api.prompt.sections import PromptContext, prompt_version, render_all

if TYPE_CHECKING:
    from lucy_api.context.types import Assembled


@dataclass(frozen=True, slots=True)
class SessionView:
    """Everything one prompt needs from storage, already read."""

    session_id: str
    items: list[dict[str, Any]]
    capabilities: tuple[str, ...] = ()
    session: dict[str, Any] | None = None
    compactions: list[dict[str, Any]] | None = None
    turn_number: int = 1


def items_from_rows(rows: list[dict[str, Any]]) -> tuple[Item, ...]:
    """Transcript rows as the projection wants them: a body, a role, a turn boundary."""
    converted: list[Item] = []
    for row in rows:
        body = row.get("content")
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        converted.append(
            Item(
                id=str(row["id"]),
                seq=int(row["seq"]),
                role=str(row["role"]),
                body=text,
                turn_id=row.get("turn_id"),
                kind=str(row.get("type") or "message"),
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

    The system string is the same document ``GET /v1/sessions/{id}/context`` returns, so a
    surprising reply can be compared to the inspectable prompt without a second code path.
    """
    built = await _build(view)
    system = built.context.text()
    if notice:
        system = f"{system}\n\n{notice}"
    return system, messages_from_items(items_from_rows(view.items))


async def _build(view: SessionView) -> Built:
    row = view.session or {}
    prompt = PromptContext(capabilities=view.capabilities)
    return await build_context(
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
            budget=BudgetSnapshot(used=0, window=200_000),
        ),
        ContextTurn(
            items=items_from_rows(view.items),
            prompt=prompt,
            compactions=compactions_from_rows(view.compactions or ()),
        ),
    )


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


# render_all is imported so a test can pin that preview and a live turn share sections.
_ = render_all

__all__ = [
    "SessionView",
    "assembled_prompt",
    "context_for_session",
    "document_from",
    "items_from_rows",
    "messages_from_items",
    "preview_document",
    "system_and_messages",
]
