"""The live runner and GET /context share one assembly path."""

from __future__ import annotations

from lucy_api.turn.prompt import (
    SessionView,
    context_for_session,
    items_from_rows,
    messages_from_items,
    preview_document,
    system_and_messages,
)


async def test_a_turn_prompt_names_the_session_the_way_context_does() -> None:
    view = SessionView(
        session_id="ses_live",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "hello",
                "turn_id": "trn_1",
                "type": "message",
            }
        ],
        capabilities=("help", "notes"),
        session={"profile": "personal", "title": "Tea", "permission_mode": "ask", "incognito": 0},
        turn_number=2,
    )
    document = await context_for_session(view)
    system, messages = await system_and_messages(view, notice="write down where you got to")

    assert document["prompt"] in system
    assert "write down where you got to" in system
    assert messages[-1].content == "hello"
    assert "notes" in document["prompt"] or "help" in document["prompt"]
    system_only, _messages = await system_and_messages(view)
    assert system_only == document["prompt"]


async def test_an_active_compaction_replaces_the_covered_turns() -> None:
    view = SessionView(
        session_id="ses_c",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "secret-turn-text",
                "turn_id": "trn_1",
                "type": "message",
            }
        ],
        session={"profile": "personal", "title": "", "permission_mode": "ask"},
        compactions=[
            {
                "seq": 1,
                "summary": "they asked about tea",
                "covers_from": 1,
                "covers_to": 1,
                "active": 1,
            }
        ],
    )
    document = await context_for_session(view)
    assert "they asked about tea" in document["prompt"]
    assert "secret-turn-text" not in document["prompt"]


def test_preview_is_the_stable_prefix_without_a_transcript() -> None:
    preview = preview_document(capabilities=("help",))
    assert preview["version"]
    assert "bands" in preview


def test_a_structured_item_is_serialised_rather_than_stringified_badly() -> None:
    items = items_from_rows(
        [
            {
                "id": "itm_1",
                "seq": 1,
                "role": "tool",
                "content": {"op": "notes.search"},
                "turn_id": "trn_1",
                "type": "tool_result",
            },
            {
                "id": "itm_2",
                "seq": 2,
                "role": "user",
                "content": "hello",
                "turn_id": "trn_1",
                "type": "message",
            },
        ]
    )
    messages = messages_from_items(items)
    assert len(messages) == 1
    assert messages[0].content == "hello"
    assert "notes.search" in items[0].body


async def test_an_inactive_compaction_does_not_hide_the_turn() -> None:
    view = SessionView(
        session_id="ses_c",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "keep-this-turn",
                "turn_id": "trn_1",
                "type": "message",
            }
        ],
        session={"profile": "personal", "title": "", "permission_mode": "ask"},
        compactions=[
            {
                "seq": 1,
                "summary": "old summary",
                "covers_from": 1,
                "covers_to": 1,
                "active": 0,
            }
        ],
    )
    document = await context_for_session(view)
    assert "keep-this-turn" in document["prompt"]
    assert "old summary" not in document["prompt"]
