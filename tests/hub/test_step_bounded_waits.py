"""A wait inside a step ends before the step does, and says the work is still running.

weftai gives every step in a plan one `stepTimeoutMs`. Seen live: `work.wait` asked to hold
for 120 seconds inside a 30-second step was cut off three times in one turn, each time with
"timed out after 30000ms. Narrow the query or raise the step timeout" -- and a waiting
`workspace.run` cut off the same way lost the handle to the command it had just started.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from weftai.operation import define_operation
from weftai.schema.spec import object_schema
from weftai.schema.types import value

from lucy_api.clients.environments import Environment, FakeEnvironmentsClient, Ran
from lucy_api.packs.base import Availability, State
from lucy_api.packs.context import STEP_MARGIN_SECONDS
from lucy_api.packs.registry import SLOW_MULTIPLE
from lucy_api.packs.service import Capabilities
from lucy_api.packs.work import WorkPack
from lucy_api.packs.workspace import WorkspacePack
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.work.registry import Registry
from lucy_api.work.types import Brief, Kind

STEP_MS = 3_000
"""A short step, so a test spends seconds and not minutes proving a wait fits inside it."""


class Stuck(FakeEnvironmentsClient):
    """A sandbox whose command is still running when anybody looks."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def run(self, *args: Any, **kwargs: Any) -> Ran:
        await self.release.wait()
        return await super().run(*args, **kwargs)


def _hub(*packs: Any, work: Registry) -> tuple[Capabilities, Any]:
    capabilities = Capabilities(packs, work=work)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    context.policy = replace(context.policy, step_timeout_ms=STEP_MS)
    return capabilities, context


async def test_a_wait_longer_than_its_step_says_still_running_instead_of_timing_out() -> None:
    """The bug, named: this step failed with a step timeout instead of answering."""
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities, context = _hub(WorkPack(), work=work)
    await capabilities.probe(context)
    handle = work.start(
        asyncio.Event().wait(),
        Brief(session_id="sess-a", kind=Kind.helper, role="r", objective="o"),
    )

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "hold", "op": "work.wait", "input": {"work_id": handle.id, "seconds": 120}}
            ]
        },
        context,
    )

    [ran] = result["steps"]
    assert ran["status"] == "ok", ran
    assert ran["data"]["status"] == "still_running"
    assert context.step_seconds == STEP_MS / 1000
    await work.shutdown()


async def test_a_waiting_command_that_outlasts_its_step_hands_back_its_handle() -> None:
    work = Registry(now=lambda: datetime.now(UTC))
    sandbox = Stuck()
    sandbox.seed(Environment("env-1", "Conversation", profile="personal"))
    capabilities, context = _hub(WorkspacePack("https://workspace.test", client=sandbox), work=work)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "run",
                    "op": "workspace.run",
                    "input": {"command": "pytest", "timeout_ms": 600_000},
                }
            ]
        },
        context,
    )

    [ran] = result["steps"]
    assert ran["status"] == "ok", ran
    assert ran["data"]["status"] == "running"
    assert ran["data"]["work_id"]
    sandbox.release.set()
    await work.shutdown()


class Research:
    """A capability named as a slow one, with nothing else to it."""

    id = "research"
    title = "Research"
    summary = "Slow, on purpose."
    docs = None

    def permissions(self) -> tuple[()]:
        return ()

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready)

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def look(_run: object) -> dict[str, bool]:
            return {"ok": True}

        return (
            define_operation(
                {
                    "name": "research.look",
                    "description": "Look.",
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "run": look,
                }
            ),
        )


async def test_a_slow_capability_in_the_plan_widens_the_step_it_waits_in() -> None:
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities, context = _hub(WorkPack(), Research(), work=work)
    capabilities.runtime_for(await capabilities.probe(context), "sess-a", context)
    assert context.step_seconds == STEP_MS / 1000 * SLOW_MULTIPLE


@pytest.mark.parametrize(
    ("step", "asked", "waits"),
    [(30.0, 120.0, 30.0 - STEP_MARGIN_SECONDS), (30.0, 5.0, 5.0), (1.0, 5.0, 0.0)],
)
def test_a_wait_is_what_was_asked_or_what_the_step_leaves(
    step: float, asked: float, waits: float
) -> None:
    capabilities, context = _hub(WorkPack(), work=Registry(now=lambda: datetime.now(UTC)))
    del capabilities
    context.step_seconds = step
    assert context.within_step(asked) == waits
