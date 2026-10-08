"""Check-ins: a subscription the hub ends itself, at a time, waking the session.

What is pinned, in order: the row and the record a check-in opens, and the timer behind it;
that firing on time and firing late say so, as the signal and in the notice; that a cancel
stops the timer and withdraws consent; that a restart takes a check-in up again and one that
fell due while Lucy was down fires at once, saying how late; that shutting down stops every
timer and keeps every row; that the sweep never asks a sibling about one; that the live block
and `work.list` say when one is due; and that `work.checkin` refuses every wrong time with
the sentence that says what to give instead.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.packs.context import PackContext, SilentTokens
from lucy_api.packs.http import NullHttp
from lucy_api.packs.registry import build_registry, build_runtime
from lucy_api.packs.work import IN_THE_PAST, NEEDS_OFFSET, NO_CHECKINS, ONE_TIME, WorkPack
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.work import Kind, State
from lucy_api.work.registry import Registry, StillRunningError
from lucy_api.work.subscriptions import (
    CHECKIN_CAPABILITY,
    CHECKIN_GRACE_SECONDS,
    CHECKIN_ROLE,
    MAX_LIFETIME_SECONDS,
    MAX_OPEN_CHECKINS,
    MIN_CHECKIN_SECONDS,
    STANDING_MARGIN_SECONDS,
    TOO_FAR,
    TOO_MANY,
    TOO_SOON,
    Subscriptions,
    SubscriptionSeam,
)
from lucy_api.work.wake import wake_line

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

ACCOUNT = "acct_checkins"
SIGNALS = "http://lucy.test/v1/signals/"
START = datetime(2026, 10, 1, 18, 0, tzinfo=UTC)
OBJECTIVE = "Look at pull request #3 again and merge it if CI passed"


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


class Clock:
    """One clock for the registry (datetimes) and the subscriptions (timestamps)."""

    def __init__(self) -> None:
        self.now = START

    def moment(self) -> datetime:
        return self.now

    def stamp(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Sleeper:
    """A sleep that records what it was asked and waits to be let go, so a test fires it."""

    def __init__(self) -> None:
        self.asked: list[float] = []
        self.gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        await self.gate.wait()

    def release(self) -> None:
        self.gate.set()


class Harness:
    def __init__(self, store: SessionStore, clock: Clock) -> None:
        self.clock = clock
        self.sleeper = Sleeper()
        self.registry = Registry(now=clock.moment)
        self.subscriptions = Subscriptions(
            store, self.registry, signal_base_url=SIGNALS, clock=clock.stamp, sleep=self.sleeper
        )
        self.consents: list[float] = []
        self.withdrawn: list[str] = []

        async def let_go(row: Any) -> None:
            self.withdrawn.append(str(row["grant_id"]))

        self.subscriptions.on_consent_release(let_go)

    async def consent(self, lifetime: float) -> str:
        self.consents.append(lifetime)
        return "dgt_checkin"

    def seam(self, session: str, *, consent: bool = True) -> SubscriptionSeam:
        return SubscriptionSeam(
            self.subscriptions,
            account_id=ACCOUNT,
            session_id=session,
            profile="personal",
            consent=self.consent if consent else None,
        )


async def a_session(store: SessionStore, key: str = "key") -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), key)
    return str(created["id"])


async def settled() -> None:
    for _ in range(20):
        await asyncio.sleep(0.005)


async def result_of(registry: Registry, work_id: str) -> Any:
    """A firing's result, waited for rather than assumed after a fixed pause.

    The firing writes through the SQLite worker thread, and a slow CI runner took longer than
    `settled()`'s tenth of a second: the result was read while the check-in still ran, and the
    test failed with "check-in is still running" on a change that never touched it.
    """
    deadline = asyncio.get_running_loop().time() + 5
    while True:
        try:
            return registry.result(work_id)
        except StillRunningError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.01)


async def rows(store: SessionStore) -> list[dict[str, Any]]:
    def read(db: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in db.execute("SELECT * FROM subscriptions").fetchall()]

    return await store.worker.call(read)


# --------------------------------------------------------------------------------------
# Opening and firing
# --------------------------------------------------------------------------------------


async def test_a_checkin_is_a_durable_subscription_with_a_timer_and_a_due_time(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    opened = await harness.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 3000, delay_seconds=3000
    )

    [record] = harness.registry.running(session)
    assert record.id == opened.handle.id
    assert (record.kind, record.role, record.wake) == (Kind.subscription, CHECKIN_ROLE, True)
    assert record.timeout_seconds == pytest.approx(3000 + CHECKIN_GRACE_SECONDS)
    assert record.tags == {
        "capability": CHECKIN_CAPABILITY,
        "subscription": opened.subscription_id,
        "grant": "dgt_checkin",
        "due": repr(clock.stamp() + 3000),
    }
    [row] = await rows(store)
    assert row["due_at"] == pytest.approx(clock.stamp() + 3000)
    assert row["capability"] == CHECKIN_CAPABILITY
    assert row["sibling_id"] is None
    assert harness.sleeper.asked == [3000]
    assert harness.consents == [3000 + CHECKIN_GRACE_SECONDS + STANDING_MARGIN_SECONDS]
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


async def test_firing_on_time_ends_it_fired_and_the_notice_says_it_is_time(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    opened = await harness.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )
    timer = harness.subscriptions._timers[opened.subscription_id]

    clock.advance(600)
    harness.sleeper.release()
    await settled()

    result = await result_of(harness.registry, opened.handle.id)
    assert result.state is State.succeeded
    assert result.payload == {
        "state": "fired",
        "summary": "It is time",
        "facts": {
            "due_at": "2026-10-01T18:10:00+00:00",
            "fired_at": "2026-10-01T18:10:00+00:00",
            "late_seconds": 0,
        },
    }
    assert result.detail == "It is time"
    [row] = await rows(store)
    assert row["state"] == "succeeded"
    assert timer.done()
    assert not timer.cancelled(), "the timer ended itself and must not cancel itself"
    assert opened.subscription_id not in harness.subscriptions._timers
    [record] = harness.registry._for(session)
    line = wake_line(record)
    assert line.startswith(f"[harness: {CHECKIN_ROLE} (subscription) - {OBJECTIVE} - succeeded")
    assert " - It is time - after 10m00s." in line
    await harness.registry.shutdown()


async def test_firing_late_says_how_late(store: SessionStore) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    opened = await harness.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )

    clock.advance(600 + 130.4)
    harness.sleeper.release()
    await settled()

    result = await result_of(harness.registry, opened.handle.id)
    assert result.payload["summary"] == "It was time 130s ago"  # type: ignore[index]
    assert result.payload["facts"]["late_seconds"] == 130  # type: ignore[index]
    await harness.registry.shutdown()


async def test_a_cancel_stops_the_timer_ends_the_row_and_withdraws_consent(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    opened = await harness.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )
    timer = harness.subscriptions._timers[opened.subscription_id]

    harness.registry.cancel(opened.handle.id)
    await settled()

    assert timer.cancelled()
    assert harness.subscriptions._timers == {}
    [row] = await rows(store)
    assert row["state"] == "cancelled"
    assert harness.withdrawn == ["dgt_checkin"]
    await harness.registry.shutdown()


@pytest.mark.parametrize(
    ("delay", "message"),
    [(MIN_CHECKIN_SECONDS - 1, TOO_SOON), (MAX_LIFETIME_SECONDS + 1, TOO_FAR)],
    ids=["too-soon", "too-far"],
)
async def test_a_time_outside_the_bounds_is_refused_before_anything_is_written(
    store: SessionStore, delay: float, message: str
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    with pytest.raises(LucyError) as refused:
        await harness.subscriptions.checkin(
            account_id=ACCOUNT,
            session_id=session,
            profile="personal",
            objective=OBJECTIVE,
            due_at=clock.stamp() + delay,
        )

    assert (refused.value.status, str(refused.value)) == (400, message)
    assert await rows(store) == []
    assert harness.registry.running(session) == ()
    await harness.registry.shutdown()


async def test_a_session_may_hold_only_so_many_checkins_and_a_fired_one_frees_a_place(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    seam = harness.seam(session)
    opened = [
        await seam.checkin(objective=f"#{n}", due_at=clock.stamp() + 600 + n, delay_seconds=600)
        for n in range(MAX_OPEN_CHECKINS)
    ]

    with pytest.raises(LucyError) as refused:
        await seam.checkin(objective="one more", due_at=clock.stamp() + 600, delay_seconds=600)
    assert (refused.value.status, str(refused.value)) == (400, TOO_MANY)
    assert len(await rows(store)) == MAX_OPEN_CHECKINS

    other = await a_session(store, key="other")
    await harness.seam(other).checkin(
        objective="elsewhere", due_at=clock.stamp() + 600, delay_seconds=600
    )

    harness.registry.cancel(opened[0].handle.id)
    await settled()
    again = await seam.checkin(
        objective="after a cancel", due_at=clock.stamp() + 600, delay_seconds=600
    )
    assert again.handle.id in {r.id for r in harness.registry.running(session)}
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


async def test_a_timer_that_fires_after_the_row_ended_elsewhere_is_logged_and_harmless(
    store: SessionStore, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    opened = await harness.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )
    harness.registry.cancel(opened.handle.id)
    await settled()

    caplog.set_level(logging.INFO, logger="lucy_api.work.subscriptions")
    harness.sleeper.release()
    await harness.subscriptions._fire(opened.subscription_id, clock.stamp() + 600)

    assert "checkin_already_ended" in caplog.text
    [row] = await rows(store)
    assert row["state"] == "cancelled"
    await harness.registry.shutdown()


async def test_the_sweep_never_asks_a_sibling_about_a_checkin(store: SessionStore) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    asked: list[Any] = []

    async def check(row: Any) -> Any:
        asked.append(row)
        return None

    harness.subscriptions.on_check(CHECKIN_CAPABILITY, check)
    await harness.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )

    assert await harness.subscriptions.sweep() == 0
    assert asked == []
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


# --------------------------------------------------------------------------------------
# Across a restart
# --------------------------------------------------------------------------------------


async def test_a_restart_before_the_due_time_arms_the_timer_again_for_what_is_left(
    store: SessionStore,
) -> None:
    clock = Clock()
    first = Harness(store, clock)
    session = await a_session(store)
    opened = await first.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 3000, delay_seconds=3000
    )
    await first.subscriptions.aclose()
    await first.registry.shutdown()
    [row] = await rows(store)
    assert row["state"] == "running", "a shutdown keeps the row for the next process"

    clock.advance(1000)
    second = Harness(store, clock)
    assert await second.subscriptions.restore() == 1
    await settled()

    [record] = second.registry.running(session)
    assert record.id == opened.handle.id
    assert record.role == CHECKIN_ROLE
    assert record.tags["due"] == repr(START.timestamp() + 3000)
    assert record.timeout_seconds == pytest.approx(2000 + CHECKIN_GRACE_SECONDS)
    assert second.sleeper.asked == [2000]

    clock.advance(2000)
    second.sleeper.release()
    await settled()
    assert (await result_of(second.registry, opened.handle.id)).payload["summary"] == "It is time"  # type: ignore[index]
    await second.registry.shutdown()


async def test_a_checkin_that_fell_due_while_lucy_was_down_fires_at_once_and_says_how_late(
    store: SessionStore,
) -> None:
    clock = Clock()
    first = Harness(store, clock)
    session = await a_session(store)
    opened = await first.seam(session).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )
    await first.subscriptions.aclose()
    await first.registry.shutdown()

    clock.advance(600 + 2 * CHECKIN_GRACE_SECONDS)  # down long past the grace
    second = Harness(store, clock)
    await second.subscriptions.restore()
    await settled()

    [record] = second.registry.running(session)
    assert record.timeout_seconds == pytest.approx(CHECKIN_GRACE_SECONDS), (
        "given its grace again, so the registry does not time it out before the timer fires"
    )
    assert second.sleeper.asked == [0.0]
    second.sleeper.release()
    await settled()
    result = await result_of(second.registry, opened.handle.id)
    assert result.state is State.succeeded
    assert result.payload["summary"] == f"It was time {int(2 * CHECKIN_GRACE_SECONDS)}s ago"  # type: ignore[index]
    await second.registry.shutdown()


async def test_closing_stops_every_timer_and_keeps_every_row(store: SessionStore) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    seam = harness.seam(session)
    for minutes in (10, 20):
        await seam.checkin(
            objective=OBJECTIVE, due_at=clock.stamp() + minutes * 60, delay_seconds=1
        )
    timers = list(harness.subscriptions._timers.values())
    assert len(timers) == 2

    await harness.subscriptions.aclose()
    await harness.subscriptions.aclose()  # twice is once

    assert all(timer.cancelled() for timer in timers)
    assert harness.subscriptions._timers == {}
    assert [row["state"] for row in await rows(store)] == ["running", "running"]
    await harness.registry.shutdown()


# --------------------------------------------------------------------------------------
# What the model sees: the live block and work.list
# --------------------------------------------------------------------------------------


async def test_the_live_block_says_when_a_checkin_is_due(store: SessionStore) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    seam = harness.seam(session)
    soon = await seam.checkin(objective="Soon", due_at=clock.stamp() + 90, delay_seconds=90)
    await seam.checkin(
        objective="Later", due_at=clock.stamp() + 2 * 86400 + 3 * 3600, delay_seconds=1
    )
    await seam.checkin(objective="Hours", due_at=clock.stamp() + 3 * 3600 + 5 * 60, delay_seconds=1)

    by_objective = {s.objective: s.progress for s in harness.registry.snapshot(session)}
    assert by_objective == {
        "Soon": "due in 1m30s",
        "Later": "due in 2d03h",
        "Hours": "due in 3h05m",
    }

    clock.advance(45)
    [snapshot] = [s for s in harness.registry.snapshot(session) if s.objective == "Soon"]
    assert snapshot.progress == "due in 45s"

    clock.advance(45)
    [snapshot] = [s for s in harness.registry.snapshot(session) if s.objective == "Soon"]
    assert snapshot.progress == "due now"

    harness.registry.cancel(soon.handle.id)
    await settled()
    [gone] = [s for s in harness.registry.snapshot(session) if s.objective == "Soon"]
    assert gone.progress == "cancelled", "a finished check-in no longer says it is due"
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


def a_context(harness: Harness, session: str, *, mode: str = "auto", seam: bool = True) -> Any:
    return PackContext(
        account_id=ACCOUNT,
        profile="personal",
        session_id=session,
        http=NullHttp(),
        tokens=SilentTokens(),
        work=harness.registry,
        permission_mode=mode,
        subscriptions=harness.seam(session) if seam else None,
    )


async def run(plan: dict[str, Any], context: Any, *, writes: bool = True) -> Any:
    runtime = build_runtime(build_registry(WorkPack().operations(context)))
    return await runtime.execute(plan, {"ctx": context, "allowWrites": writes})


def checkin(**inputs: Any) -> dict[str, Any]:
    return {
        "steps": [{"id": "c", "op": "work.checkin", "input": {"objective": OBJECTIVE, **inputs}}]
    }


async def test_work_checkin_opens_one_and_work_list_shows_when_it_is_due(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)
    context = a_context(harness, session)

    result = await run(checkin(in_seconds=2700), context)
    [step] = result["steps"]
    assert step["data"]["due_at"] == "2026-10-01T18:45:00+00:00"
    assert step["data"]["in_seconds"] == 2700
    assert "work.cancel" in step["data"]["advice"]
    work_id = step["data"]["id"]

    clock.advance(60)
    listed = await run({"steps": [{"id": "l", "op": "work.list", "input": {}}]}, context)
    [line] = listed["steps"][0]["data"]["running"]
    assert line["id"] == work_id
    assert (line["role"], line["due_at"], line["due_in_seconds"]) == (
        CHECKIN_ROLE,
        "2026-10-01T18:45:00+00:00",
        2640,
    )
    assert line["progress"] == ""
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


async def test_a_minimum_checkin_survives_time_spent_recording_consent(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    async def slow_consent(lifetime: float) -> str:
        clock.advance(0.1)
        return await harness.consent(lifetime)

    context = a_context(harness, session)
    context.subscriptions = SubscriptionSeam(
        harness.subscriptions,
        account_id=ACCOUNT,
        session_id=session,
        profile="personal",
        consent=slow_consent,
    )
    result = await run(checkin(in_seconds=MIN_CHECKIN_SECONDS), context)
    data = result["steps"][0]["data"]
    assert data["in_seconds"] == MIN_CHECKIN_SECONDS
    assert data["due_at"] == "2026-10-01T18:01:00+00:00"
    assert len(await rows(store)) == 1
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


async def test_work_checkin_takes_a_moment_with_its_offset(store: SessionStore) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    result = await run(checkin(at="2026-10-01T20:24:00+01:00"), a_context(harness, session))

    data = result["steps"][0]["data"]
    assert data["due_at"] == "2026-10-01T19:24:00+00:00"
    assert data["in_seconds"] == 84 * 60
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()


@pytest.mark.parametrize(
    ("inputs", "message"),
    [
        ({"in_seconds": 600, "at": "2026-10-01T19:00:00Z"}, ONE_TIME),
        ({}, ONE_TIME),
        ({"at": "2026-10-01T19:00:00"}, NEEDS_OFFSET),
        ({"at": "tomorrow at nine"}, NEEDS_OFFSET),
        ({"at": "2026-10-01T17:00:00Z"}, IN_THE_PAST),
        ({"in_seconds": 0}, IN_THE_PAST),
        ({"in_seconds": 30}, TOO_SOON),
        ({"in_seconds": 8 * 86400}, TOO_FAR),
        ({"in_seconds": 600, "objective": "   "}, "Say in `objective`"),
    ],
    ids=["both", "neither", "naive", "prose", "past", "now", "too-soon", "too-far", "no-objective"],
)
async def test_work_checkin_refuses_a_wrong_time_with_the_sentence_that_fixes_it(
    store: SessionStore, inputs: dict[str, Any], message: str
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    result = await run(checkin(**inputs), a_context(harness, session))

    data = result["steps"][0]["data"]
    assert data["status"] == "invalid"
    assert message in data["message"]
    assert await rows(store) == []
    await harness.registry.shutdown()


async def test_without_a_subscription_seam_a_checkin_says_it_cannot_come_back(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    result = await run(checkin(in_seconds=600), a_context(harness, session, seam=False))

    assert result["steps"][0]["data"] == {"status": "unavailable", "message": NO_CHECKINS}
    await harness.registry.shutdown()


async def test_a_checkin_is_a_write_and_is_refused_whole_in_a_read_only_turn(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    result = await run(checkin(in_seconds=600), a_context(harness, session), writes=False)

    assert result["issues"]
    assert await rows(store) == []
    await harness.registry.shutdown()


async def test_a_seam_without_consent_still_opens_a_checkin_that_cannot_act(
    store: SessionStore,
) -> None:
    clock = Clock()
    harness = Harness(store, clock)
    session = await a_session(store)

    opened = await harness.seam(session, consent=False).checkin(
        objective=OBJECTIVE, due_at=clock.stamp() + 600, delay_seconds=600
    )

    [record] = harness.registry.running(session)
    assert "grant" not in record.tags
    assert opened.handle.id == record.id
    await harness.subscriptions.aclose()
    await harness.registry.shutdown()
