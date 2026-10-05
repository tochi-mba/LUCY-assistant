"""Work past a cap waits its turn, and a group of work ends once.

What is pinned here is what cannot happen: queued work cannot start early, cannot run past
the number of slots its kind was given, cannot overtake work queued before it, cannot start
while the process is going down, and cannot wait without bound. A group cannot be announced
before its last member ends, or twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.work import (
    AtCapacityError,
    Brief,
    Kind,
    Record,
    Registry,
    State,
    StillRunningError,
    Team,
)
from lucy_api.work.registry import CANCELLED_QUEUED

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

SESSION = "ses_queue"
START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def a_brief(**overrides: Any) -> Brief:
    fields: dict[str, Any] = {
        "session_id": SESSION,
        "kind": Kind.helper,
        "role": "reviewer",
        "objective": "Read the draft through one lens",
    }
    return Brief(**{**fields, **overrides})


async def settled() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


class Gates:
    """Work that runs until a test lets it end, counting how many run at once."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.running = 0
        self.most = 0

    def work(self, name: str) -> Callable[[], Awaitable[object]]:
        gate = self.gates.setdefault(name, asyncio.Event())

        async def run() -> str:
            self.started.append(name)
            self.running += 1
            self.most = max(self.most, self.running)
            try:
                await gate.wait()
            finally:
                self.running -= 1
            return f"{name} done"

        return run

    def release(self, name: str) -> None:
        self.gates[name].set()


async def test_work_with_a_free_slot_starts_at_once_and_past_the_slots_it_queues() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    first = registry.queue(gates.work("a"), a_brief(), slots=2, waiting=2)
    second = registry.queue(gates.work("b"), a_brief(), slots=2, waiting=2)
    third = registry.queue(gates.work("c"), a_brief(), slots=2, waiting=2)
    await settled()

    assert gates.started == ["a", "b"]
    assert registry.state_of(first.id) is State.running
    assert registry.state_of(second.id) is State.running
    assert registry.state_of(third.id) is State.queued
    assert [record.id for record in registry.running(SESSION)] == [first.id, second.id, third.id]
    shown = {work.id: work.status for work in registry.snapshot(SESSION)}
    assert shown[third.id] == "queued"
    for name in ("a", "b", "c"):
        gates.release(name)
    await settled()


async def test_queued_work_starts_in_order_as_slots_free_and_never_past_them() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    handles = [
        registry.queue(gates.work(name), a_brief(role=name), slots=2, waiting=5)
        for name in ("a", "b", "c", "d", "e")
    ]
    await settled()
    assert gates.started == ["a", "b"]

    gates.release("b")
    await settled()
    assert gates.started == ["a", "b", "c"]
    gates.release("a")
    await settled()
    assert gates.started == ["a", "b", "c", "d"]
    for name in ("c", "d", "e"):
        gates.release(name)
        await settled()

    assert gates.started == ["a", "b", "c", "d", "e"]
    assert gates.most == 2
    assert all(registry.state_of(handle.id) is State.succeeded for handle in handles)


async def test_queued_work_is_not_made_until_it_starts_and_its_clock_starts_then() -> None:
    """The helper's wall clock is the work's own: nothing of it exists while it waits."""
    clock = Clock()
    registry = Registry(now=clock)
    gates = Gates()
    made: list[str] = []

    def counted(name: str) -> Callable[[], Awaitable[object]]:
        inner = gates.work(name)

        def make() -> Awaitable[object]:
            made.append(name)
            return inner()

        return make

    registry.queue(counted("a"), a_brief(), slots=1, waiting=1)
    waiting = registry.queue(counted("b"), a_brief(), slots=1, waiting=1)
    assert made == ["a"]
    clock.advance(90)
    [queued] = [work for work in registry.snapshot(SESSION) if work.id == waiting.id]
    assert queued.elapsed_seconds == 90

    gates.release("a")
    await settled()
    assert made == ["a", "b"]
    [started] = [work for work in registry.snapshot(SESSION) if work.id == waiting.id]
    assert started.status == "running"
    assert started.elapsed_seconds == 0
    gates.release("b")
    await settled()


async def test_the_queue_is_bounded_and_the_refusal_says_what_to_do() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=1)
    registry.queue(gates.work("b"), a_brief(), slots=1, waiting=1)
    made: list[str] = []

    def never() -> Awaitable[object]:
        made.append("c")
        return asyncio.sleep(0)

    with pytest.raises(AtCapacityError) as refused:
        registry.queue(never, a_brief(), slots=1, waiting=1)

    assert "1 helpers may run at once and 1 more are already queued" in str(refused.value)
    assert "cancel one, or do this inline" in str(refused.value)
    assert made == []
    gates.release("a")
    gates.release("b")
    await settled()


async def test_the_registry_cap_holds_queued_work_even_when_its_kind_has_room() -> None:
    registry = Registry(now=Clock(), max_concurrent=1)
    gates = Gates()
    registry.start(gates.work("job")(), a_brief(kind=Kind.job, role="download"))
    helper = registry.queue(gates.work("helper"), a_brief(), slots=3, waiting=3)
    await settled()
    assert registry.state_of(helper.id) is State.queued

    gates.release("job")
    await settled()
    assert registry.state_of(helper.id) is State.running
    gates.release("helper")
    await settled()


async def test_other_kinds_do_not_take_a_helper_slot_and_queued_work_is_not_counted() -> None:
    registry = Registry(now=Clock(), max_concurrent=3)
    gates = Gates()
    registry.start(gates.work("job")(), a_brief(kind=Kind.job, role="download"))
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=2)
    registry.queue(gates.work("b"), a_brief(), slots=1, waiting=2)
    registry.queue(gates.work("c"), a_brief(), slots=1, waiting=2)
    await settled()
    assert gates.started == ["job", "a"]
    # Two queued helpers are not two running things: a third kind still has room.
    registry.start(gates.work("cmd")(), a_brief(kind=Kind.command, role="tests"))
    await settled()
    assert gates.started == ["job", "a", "cmd"]
    for name in ("job", "a", "b", "c", "cmd"):
        gates.release(name)
        await settled()


async def test_cancelling_queued_work_ends_it_unstarted_and_tells_whoever_queued_it() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    told: list[str] = []
    ended: list[Record] = []

    async def dropped() -> None:
        told.append("dropped")

    async def listener(record: Record) -> None:
        ended.append(record)

    registry.on_finished(listener)
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=2)
    waiting = registry.queue(gates.work("b"), a_brief(), slots=1, waiting=2, dropped=dropped)
    after = registry.queue(gates.work("c"), a_brief(), slots=1, waiting=2)

    record = registry.cancel(waiting.id)
    again = registry.cancel(waiting.id)
    await settled()

    assert record.state is State.cancelled
    assert record.detail == CANCELLED_QUEUED
    assert again is record
    assert told == ["dropped"]
    assert [item.id for item in ended] == [waiting.id]
    gates.release("a")
    await settled()
    assert gates.started == ["a", "c"]
    assert registry.state_of(after.id) is State.running
    gates.release("c")
    await settled()
    assert "b" not in gates.started


async def test_a_dropped_callback_that_fails_is_logged_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = Registry(now=Clock())
    gates = Gates()

    async def broken() -> None:
        raise RuntimeError

    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=1)
    waiting = registry.queue(gates.work("b"), a_brief(), slots=1, waiting=1, dropped=broken)
    with caplog.at_level(logging.WARNING):
        registry.cancel(waiting.id)
        await settled()

    assert "work dropped callback failed" in caplog.text
    gates.release("a")
    await settled()


async def test_queued_work_cancelled_without_a_callback_just_ends() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=1)
    waiting = registry.queue(gates.work("b"), a_brief(), slots=1, waiting=1)
    assert registry.cancel(waiting.id).state is State.cancelled
    gates.release("a")
    await settled()


async def test_a_queued_result_is_not_there_yet_and_says_it_is_queued() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=1)
    waiting = registry.queue(gates.work("b"), a_brief(), slots=1, waiting=1)

    with pytest.raises(StillRunningError, match="still queued; wait for its notice"):
        registry.result(waiting.id)
    with pytest.raises(StillRunningError, match="still queued after 0s"):
        await registry.wait(waiting.id, 0)
    gates.release("a")
    gates.release("b")
    await settled()


async def test_an_id_already_taken_is_refused_before_anything_is_made() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=1, work_id="wrk_same")
    with pytest.raises(ValueError, match="already registered"):
        registry.queue(gates.work("b"), a_brief(), slots=1, waiting=1, work_id="wrk_same")
    gates.release("a")
    await settled()


async def test_nothing_queued_starts_while_the_process_goes_down() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    registry.queue(gates.work("a"), a_brief(), slots=1, waiting=1)
    waiting = registry.queue(gates.work("b"), a_brief(), slots=1, waiting=1)
    await settled()

    await registry.shutdown()

    assert gates.started == ["a"]
    assert registry.state_of(waiting.id) is State.queued


# --------------------------------------------------------------------------------------
# Groups
# --------------------------------------------------------------------------------------


async def test_a_group_ends_once_when_its_last_member_ends_naming_every_member() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    teams: list[Team] = []

    async def listener(team: Team) -> None:
        teams.append(team)

    registry.on_team_finished(listener)
    members = [
        registry.queue(gates.work(name), a_brief(role=name, group="reviewers"), slots=2, waiting=2)
        for name in ("protocol", "platform", "onboarding")
    ]
    loner = registry.queue(gates.work("solo"), a_brief(role="solo"), slots=5, waiting=0)

    gates.release("protocol")
    await settled()
    gates.release("platform")
    await settled()
    assert teams == []
    registry.cancel(members[2].id)
    gates.release("solo")
    await settled()

    [team] = teams
    assert team.group == "reviewers"
    assert team.ids == tuple(handle.id for handle in members)
    assert loner.id not in team.ids
    assert team.line().startswith("group reviewers (3 members) has ended: protocol ")
    assert "platform " in team.line()
    assert f"onboarding {members[2].id} - cancelled" in team.line()
    assert registry.drain_teams(SESSION) == (team,)
    assert registry.drain_teams(SESSION) == ()


async def test_a_group_reused_after_it_ended_is_a_new_team() -> None:
    registry = Registry(now=Clock())
    gates = Gates()
    first = registry.queue(gates.work("a"), a_brief(group="skeptics"), slots=5, waiting=0)
    gates.release("a")
    await settled()
    second = registry.queue(gates.work("b"), a_brief(group="skeptics"), slots=5, waiting=0)
    gates.release("b")
    await settled()

    teams = registry.drain_teams(SESSION)
    assert [team.ids for team in teams] == [(first.id,), (second.id,)]
    assert teams[0].line().startswith("group skeptics (1 member) has ended")


async def test_a_failing_team_listener_is_logged_by_group_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = Registry(now=Clock())

    async def broken(_team: Team) -> None:
        raise RuntimeError

    async def boom() -> str:
        raise ValueError

    registry.on_team_finished(broken)
    with caplog.at_level(logging.WARNING):
        registry.start(boom(), a_brief(group="researchers"))
        await settled()

    assert "work team listener failed" in caplog.text
    [team] = registry.drain_teams(SESSION)
    assert "ValueError" in team.line()


async def test_work_lost_to_a_restart_never_forms_a_team_on_its_own() -> None:
    registry = Registry(now=Clock(), keep_finished=1)
    registry.record_lost(
        a_brief(group="reviewers"),
        work_id="wrk_lost",
        started_at=START,
        ended_at=START,
        detail="restart",
    )
    gates = Gates()
    fresh = registry.queue(gates.work("a"), a_brief(group="reviewers"), slots=1, waiting=0)
    gates.release("a")
    await settled()

    [team] = registry.drain_teams(SESSION)
    assert team.ids == (fresh.id,)


async def test_a_session_keeps_only_as_many_untold_teams_as_finished_records() -> None:
    registry = Registry(now=Clock(), keep_finished=1)
    for name in ("one", "two"):
        registry.start(asyncio.sleep(0, result=name), a_brief(group=name))
        await settled()
    [kept] = registry.drain_teams(SESSION)
    assert kept.group == "two"


def test_a_team_speaks_for_its_members() -> None:
    def member(identifier: str, **overrides: Any) -> Record:
        fields: dict[str, Any] = {
            "id": identifier,
            "kind": Kind.helper,
            "role": "skeptic",
            "objective": "Refute one finding",
            "session_id": SESSION,
            "started_at": START,
            "state": State.succeeded,
        }
        return Record(**{**fields, **overrides})

    quiet = Team(SESSION, "skeptics", (member("a"), member("b", tokens=1200)))
    assert not quiet.wake
    assert quiet.account_id == ""
    assert not quiet.fetched
    assert quiet.line() == (
        "group skeptics (2 members) has ended: skeptic a - succeeded; "
        "skeptic b - succeeded - about 1,200 tokens of result"
    )
    loud = Team(
        SESSION,
        "skeptics",
        (member("a", wake=True, account_id="acct", fetched=True), member("b", fetched=True)),
    )
    assert loud.wake
    assert loud.account_id == "acct"
    assert loud.fetched


def test_a_notice_names_the_group_its_work_was_started_in() -> None:
    record = Record(
        id="wrk_1",
        kind=Kind.helper,
        role="reviewer",
        objective="Read it",
        session_id=SESSION,
        started_at=START,
        finished_at=START,
        state=State.failed,
        detail="stopped",
        group="reviewers",
    )
    assert (
        record.notice(START).line()
        == f"reviewer (helper, reviewers) - Read it - failed - id {record.id} - stopped"
    )
    assert record.notice(START).group == "reviewers"
