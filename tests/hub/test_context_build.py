"""One turn's context, from storage and live systems to the bytes the model reads.

The pieces are tested on their own elsewhere. What is pinned here is that they compose:
that a failing journal reaches the model as a line it can act on rather than as a stack
trace, and that a compacted conversation and a flood of tool output still leave Lucy
knowing what time it is and which of its agents are still working.
"""

from __future__ import annotations

from datetime import UTC, datetime

from lucy_api.context.build import LIVE_SHARE, Sources, Turn, build_context
from lucy_api.context.projection import Compaction, Item
from lucy_api.context.sources import StateRequest
from lucy_api.context.types import (
    AgentSnapshot,
    Band,
    Budget,
    BudgetSnapshot,
    Section,
    SessionSnapshot,
    TopicSnapshot,
)

NOW = datetime(2026, 9, 17, 14, 32, tzinfo=UTC)
SESSION = SessionSnapshot(
    id="ses_1", profile="personal", title="Tour dates", turn_number=42, permission_mode="ask"
)
REQUEST = StateRequest(
    now=NOW, session=SESSION, budget=BudgetSnapshot(used=84_000, window=200_000, reclaimable=6)
)


class Gives:
    def __init__(self, value) -> None:
        self.value = value

    async def fetch(self, session_id: str):
        return self.value


class Breaks:
    async def fetch(self, session_id: str):
        raise TimeoutError("the journal is restarting")


def conversation(turns: int) -> list[Item]:
    return [
        Item(
            id=f"i{number}",
            seq=number,
            role="user" if number % 2 else "assistant",
            body=f"entry {number}",
            turn_id=f"trn_{number}",
        )
        for number in range(1, turns + 1)
    ]


async def test_a_whole_turn_assembles_with_the_live_state_read_last() -> None:
    built = await build_context(
        REQUEST,
        Turn(items=conversation(4)),
        sources=Sources(
            agents=Gives(
                [
                    AgentSnapshot(
                        id="agt_1",
                        role="researcher",
                        objective="Find the tour dates",
                        status="running",
                    )
                ]
            )
        ),
    )
    sections = built.context.sections
    assert sections[-1].id.startswith("live"), "the block that changes every turn goes last"
    assert "Find the tour dates" in sections[-1].body
    assert "entry 1" in built.context.text()
    assert built.live_tokens > 0


async def test_a_journal_that_is_down_becomes_a_line_the_model_can_read() -> None:
    built = await build_context(
        REQUEST,
        Turn(),
        sources=Sources(tasks=Breaks(), agents=Gives([])),
    )
    live = built.context.sections[-1].body
    assert "journal" in live
    assert "unavailable" in live
    assert "TimeoutError" in live, "the kind of failure is named"
    assert "restarting" not in live, "the message it carried is not"


async def test_every_source_failing_still_produces_a_usable_prompt() -> None:
    built = await build_context(
        REQUEST,
        Turn(items=conversation(2)),
        sources=Sources(
            agents=Breaks(),
            tasks=Breaks(),
            workspace=Breaks(),
            capabilities=Breaks(),
            topics=Breaks(),
            pending=Breaks(),
        ),
    )
    text = built.context.text()
    assert "entry 1" in text, "the conversation survived"
    assert "2026-09-17" in text, "so did the date"
    assert built.context.total > 0


async def test_the_live_block_stays_inside_its_share_of_the_pinned_band() -> None:
    budget = Budget(window=200_000)
    crowd = [
        AgentSnapshot(
            id=f"agt_{index}",
            role="researcher",
            objective=f"Investigate subject number {index} in considerable detail",
            status="running",
            progress="still going",
        )
        for index in range(60)
    ]
    topics = [
        TopicSnapshot(
            id=f"top_{index}",
            title=f"Subject {index}",
            summary="Something worth remembering about this particular subject",
            count=index,
        )
        for index in range(60)
    ]
    built = await build_context(
        REQUEST,
        Turn(),
        sources=Sources(agents=Gives(crowd), topics=Gives(topics)),
        budget=budget,
    )
    ceiling = int(budget.allocation(Band.pinned) * LIVE_SHARE)
    assert built.live_tokens <= ceiling
    assert "60 agents running" in built.context.sections[-1].body, "the crowd is counted honestly"


async def test_a_compacted_conversation_reads_as_its_summary() -> None:
    built = await build_context(
        REQUEST,
        Turn(
            items=conversation(6),
            compactions=[
                Compaction(seq=1, summary="They asked about tours.", covers_from=1, covers_to=4)
            ],
        ),
    )
    text = built.context.text()
    assert built.replaced_items == 4
    assert "They asked about tours." in text
    assert "entry 1" not in text
    assert "entry 5" in text, "what was not summarised is still there in full"


async def test_a_flood_of_tool_results_cannot_cost_lucy_its_own_state() -> None:
    flood = [
        Section(id=f"tool.{index}", band=Band.tools, body="x" * 50_000, tokens=0)
        for index in range(30)
    ]
    built = await build_context(
        REQUEST,
        Turn(items=conversation(2), tools=flood),
        sources=Sources(
            agents=Gives(
                [AgentSnapshot(id="a", role="reviewer", objective="Check it", status="running")]
            )
        ),
    )
    live = built.context.sections[-1].body
    assert "Check it" in live
    assert "1 agent running" in live
    assert built.notices, "and what did not fit was said out loud"
    assert built.notices == built.context.notices


async def test_with_no_live_systems_at_all_the_turn_still_knows_where_it_is() -> None:
    built = await build_context(REQUEST, Turn(items=conversation(1)))
    live = built.context.sections[-1].body
    assert "ses_1" in live
    assert "turn 42" in live
    assert "84,000 of 200,000" in live
    assert "agents" not in live, "a group with nothing in it is left out, not printed empty"
