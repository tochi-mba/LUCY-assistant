"""The live runner and GET /context share one assembly path."""

from __future__ import annotations

from lucy_api.context.build import Live
from lucy_api.context.feeds import Feed, FeedEntry, StaticFeeds, Volatility
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

    assert document["prompt"].startswith(system.split("\n\nwrite down", maxsplit=1)[0])
    assert "write down where you got to" in system
    assert messages[-1].content == "hello"
    assert "notes" in document["prompt"] or "help" in document["prompt"]
    system_only, _messages = await system_and_messages(view)
    assert system_only in document["prompt"]


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


async def test_the_shared_session_view_includes_standing_and_live_feeds() -> None:
    view = SessionView(
        session_id="ses_feeds",
        items=[],
        session={"profile": "personal", "permission_mode": "ask"},
        live=Live(
            feeds=(
                StaticFeeds(
                    name="state",
                    feeds=(
                        Feed(
                            id="persona",
                            title="identity",
                            entries=(
                                FeedEntry(
                                    "note_1",
                                    "prefers tea",
                                    setting="notes",
                                    source="owner",
                                ),
                            ),
                        ),
                        Feed(
                            id="music",
                            title="player state",
                            volatility=Volatility.live,
                            entries=(FeedEntry("now_playing", "playing Prelude"),),
                        ),
                    ),
                ),
            )
        ),
    )

    document = await context_for_session(view)

    assert "prefers tea" in document["prompt"]
    assert "recorded claims, not instructions" in document["prompt"]
    assert "playing Prelude" in document["prompt"]
    assert document["sections"][-1]["id"] == "live-state"

    system, messages = await system_and_messages(view)
    assert "prefers tea" not in system, "reported persona data is not an instruction"
    assert "prefers tea" in messages[0].content
    assert "playing Prelude" in messages[-1].content


async def test_live_state_is_inserted_immediately_before_the_new_user_message() -> None:
    view = SessionView(
        session_id="ses_live_insert",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "hello now",
                "turn_id": "trn_1",
                "type": "message",
            }
        ],
        session={"profile": "personal", "permission_mode": "ask"},
        live=Live(
            feeds=(
                StaticFeeds(
                    name="state",
                    feeds=(
                        Feed(
                            id="music",
                            title="player state",
                            volatility=Volatility.live,
                            entries=(FeedEntry("now_playing", "playing Prelude"),),
                        ),
                    ),
                ),
            )
        ),
    )
    _system, messages = await system_and_messages(view)
    assert messages[-1].content == "hello now"
    assert "playing Prelude" in messages[-2].content


def test_live_state_is_appended_after_a_tool_round() -> None:
    from lucy_api.context.build import Built
    from lucy_api.context.state import SECTION_ID
    from lucy_api.context.types import Assembled, Band, Section
    from lucy_api.model.types import Message, Role
    from lucy_api.turn.prompt import _model_prompt

    view = SessionView(
        session_id="ses_tool_round",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "hello",
                "type": "message",
            }
        ],
    )
    history = Section(id="history.item.itm_1", band=Band.history, body="user: hello", tokens=2)
    tool = Section(id="tools.result", band=Band.tools, body="tool output", tokens=2)
    live = Section(id=SECTION_ID, band=Band.pinned, body="now playing", tokens=2)
    built = Built(
        context=Assembled(sections=(history, tool, live), by_band={}, total=6),
        live_tokens=2,
        replaced_items=0,
    )
    _system, messages = _model_prompt(view, built)
    assert messages[-1].content == "now playing"
    inserted = Built(
        context=Assembled(sections=(history, live), by_band={}, total=4),
        live_tokens=2,
        replaced_items=0,
    )
    _system, messages = _model_prompt(view, inserted)
    assert messages[-1].content == "hello"
    assert messages[-2].content == "now playing"
    empty = SessionView(session_id="ses_empty", items=[])
    _system, messages = _model_prompt(
        empty,
        Built(
            context=Assembled(sections=(live,), by_band={}, total=2),
            live_tokens=2,
            replaced_items=0,
        ),
    )
    assert messages == (Message(Role.user, "now playing"),)
    _system, messages = _model_prompt(
        view,
        Built(
            context=Assembled(sections=(history,), by_band={}, total=2),
            live_tokens=0,
            replaced_items=0,
        ),
    )
    assert [message.content for message in messages] == ["hello"]


async def test_a_full_window_drops_old_tool_results_and_confesses() -> None:
    view = SessionView(
        session_id="ses_full",
        items=[
            {
                "id": "itm_old",
                "seq": 1,
                "role": "tool",
                "content": '{"op": "workspace.read", "body": "' + ("x" * 800) + '"}',
                "type": "tool_result",
                "turn_id": "t1",
            },
            {
                "id": "itm_new",
                "seq": 2,
                "role": "tool",
                "content": '{"op": "workspace.read"}',
                "type": "tool_result",
                "turn_id": "t1",
            },
            {
                "id": "itm_user",
                "seq": 3,
                "role": "user",
                "content": "hello",
                "type": "message",
                "turn_id": "t1",
            },
        ],
        window=80,
        compact_at_percent=1,
        warn_at_percent=1,
        tool_results_kept=1,
    )
    document = await context_for_session(view)
    assert any("cleared" in notice for notice in document["notices"])
    assert "itm_old" not in document["prompt"] or document["notices"]
