"""Work in flight is announced on a real turn, not when somebody previews the prompt."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lucy_api.context.build import Live
from lucy_api.context.sources import Sources
from lucy_api.turn.supervisor import _announced
from lucy_api.work.live import WorkInFlight
from lucy_api.work.registry import Registry
from lucy_api.work.types import Brief, Kind

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


async def test_a_preview_does_not_eat_the_just_finished_flag() -> None:
    finished = asyncio.Event()

    async def done() -> str:
        finished.set()
        return "ok"

    registry = Registry(now=lambda: NOW)
    registry.start(
        done(),
        Brief(session_id="ses", kind=Kind.helper, role="reviewer", objective="Check"),
    )
    await finished.wait()
    preview = WorkInFlight(registry, announce=False)
    turn = WorkInFlight(registry, announce=True)

    first = await preview.fetch("ses")
    second = await preview.fetch("ses")
    announced = await turn.fetch("ses")
    after = await turn.fetch("ses")

    assert first[0].finished_since_last_turn is True
    assert second[0].finished_since_last_turn is True
    assert announced[0].finished_since_last_turn is True
    assert after == ()


def test_a_real_turn_announces_work_without_dropping_the_rest_of_live_state() -> None:
    class Topics:
        async def fetch(self, session_id: str) -> tuple[object, ...]:
            return ()

    topics = Topics()
    live = Live(sources=Sources(topics=topics))
    registry = Registry(now=lambda: NOW)

    assert _announced(None, None) is None
    announced = _announced(live, registry)

    assert announced is not None
    assert announced.sources is not None
    assert announced.sources.topics is topics
    assert isinstance(announced.sources.in_flight, WorkInFlight)
    assert announced.sources.in_flight.announce is True
