"""What the live state does when the world it describes is only partly available.

The rule being pinned down here is the one that matters on every single turn: a subsystem
that is missing costs its own group and nothing else. A turn is never failed because the
task journal was restarting.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from lucy_api.context.sources import Sources, StateRequest, gather_live_state
from lucy_api.context.types import (
    AgentSnapshot,
    BudgetSnapshot,
    CapabilitySnapshot,
    PendingSnapshot,
    SessionSnapshot,
    TaskSnapshot,
    TopicSnapshot,
    WorkspaceSnapshot,
)

NOW = datetime(2026, 9, 17, 14, 30, tzinfo=UTC)
SESSION = SessionSnapshot(
    id="ses_1", profile="personal", title="Tour dates", turn_number=4, permission_mode="ask"
)
BUDGET = BudgetSnapshot(used=84_000, window=200_000, reclaimable=6)


class Gives:
    """A source that hands back whatever it was built with."""

    def __init__(self, value, *, delay: float = 0.0) -> None:
        self.value = value
        self.delay = delay
        self.asked_for: list[str] = []

    async def fetch(self, session_id: str):
        self.asked_for.append(session_id)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.value


class Breaks:
    """A source that is deployed and not working, which is the interesting case."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or TimeoutError("took too long")

    async def fetch(self, session_id: str):
        raise self.error


def request(**overrides) -> StateRequest:
    return StateRequest(now=NOW, session=SESSION, budget=BUDGET, **overrides)


async def test_with_nothing_deployed_the_state_is_still_a_usable_state() -> None:
    state = await gather_live_state(request(), Sources())
    assert state.session is SESSION
    assert state.budget is BUDGET
    assert state.now == NOW
    assert state.agents == ()
    assert state.tasks == ()
    assert state.topics == ()
    assert state.capabilities == ()
    assert state.workspace is None
    assert state.pending == PendingSnapshot()
    assert state.failures == (), "a subsystem nobody installed is not a problem to report"


async def test_every_source_reaches_the_state_it_belongs_to() -> None:
    agent = AgentSnapshot(
        id="agt_1", role="researcher", objective="Find tour dates", status="running"
    )
    task = TaskSnapshot(id="tsk_1", title="Check the venue", status="pending")
    topic = TopicSnapshot(id="top_1", title="Tea", summary="Prefers Earl Grey", count=3)
    capability = CapabilitySnapshot(id="music", title="Music", state="ready")
    workspace = WorkspaceSnapshot(path="/w", ready=True)
    pending = PendingSnapshot(approvals=("apr_1",))

    state = await gather_live_state(
        request(),
        Sources(
            agents=Gives([agent]),
            tasks=Gives([task]),
            workspace=Gives(workspace),
            capabilities=Gives([capability]),
            topics=Gives([topic]),
            pending=Gives(pending),
        ),
    )
    assert state.agents == (agent,)
    assert state.tasks == (task,)
    assert state.workspace is workspace
    assert state.capabilities == (capability,)
    assert state.topics == (topic,)
    assert state.pending is pending
    assert state.running_agents == (agent,)


async def test_a_broken_source_costs_its_own_group_and_no_other() -> None:
    agent = AgentSnapshot(id="agt_1", role="reviewer", objective="Check it", status="running")
    state = await gather_live_state(request(), Sources(agents=Gives([agent]), tasks=Breaks()))
    assert state.agents == (agent,), "the working source still answered"
    assert state.tasks == ()
    assert [failure.operation for failure in state.failures] == ["journal"]
    assert "unavailable" in state.failures[0].detail


async def test_the_report_names_the_kind_of_failure_and_never_the_payload() -> None:
    secret = "sk-live-abcdefghijklmnop"
    state = await gather_live_state(request(), Sources(topics=Breaks(RuntimeError(secret))))
    detail = state.failures[0].detail
    assert "RuntimeError" in detail
    assert secret not in detail, "a failing source must not leak what it was carrying"


async def test_several_broken_sources_are_each_reported_once() -> None:
    state = await gather_live_state(
        request(),
        Sources(agents=Breaks(), tasks=Breaks(), workspace=Breaks(), capabilities=Breaks()),
    )
    assert sorted(failure.operation for failure in state.failures) == [
        "agents",
        "capabilities",
        "journal",
        "workspace",
    ]
    assert all(failure.count == 1 for failure in state.failures)


async def test_failures_the_caller_already_knew_about_are_kept() -> None:
    from lucy_api.context.types import FailureSnapshot

    known = FailureSnapshot(operation="research.search", count=2, detail="429")
    state = await gather_live_state(request(failures=(known,)), Sources(tasks=Breaks()))
    assert state.failures[0] is known
    assert [failure.operation for failure in state.failures] == ["research.search", "journal"]


async def test_every_source_is_asked_about_this_session_and_no_other() -> None:
    agents, tasks = Gives([]), Gives([])
    await gather_live_state(request(), Sources(agents=agents, tasks=tasks))
    assert agents.asked_for == ["ses_1"]
    assert tasks.asked_for == ["ses_1"]


async def test_the_sources_are_fetched_together_rather_than_one_after_another() -> None:
    started: list[str] = []
    finished: list[str] = []

    class Timed:
        def __init__(self, name: str, delay: float) -> None:
            self.name, self.delay = name, delay

        async def fetch(self, session_id: str):
            started.append(self.name)
            await asyncio.sleep(self.delay)
            finished.append(self.name)
            return []

    await gather_live_state(
        request(),
        Sources(agents=Timed("agents", 0.02), tasks=Timed("tasks", 0.01)),
    )
    assert started == ["agents", "tasks"], "both began before either had finished"
    assert finished == ["tasks", "agents"], "the quicker one did not wait for the slower"


async def test_a_cancelled_turn_is_not_swallowed_by_a_source() -> None:
    """A cancellation means nobody is waiting for this prompt any more.

    Catching it here would leave the assembler quietly finishing work for a turn that has
    been abandoned, which is how a cancelled request keeps costing money.
    """
    with pytest.raises(asyncio.CancelledError):
        await gather_live_state(request(), Sources(agents=Breaks(asyncio.CancelledError())))
