"""Quiet hours: an ending inside the person's window is told when it closes, never lost.

What is pinned, in order: how a window is read (on the person's clock, wrapping midnight,
across a change of offset); which endings are quiet; that a quiet ending is announced at once
and its turn deferred to a check-in at the window's end, which tells that ending -- or nothing,
if the person read it meanwhile or called the check-in off; that a deferral which cannot be
recorded wakes now rather than never; and that the check-in is durable and carries what a
restarted process needs to tell the person on its own.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.stream.events import WORK_FINISHED
from lucy_api.work import Kind, Record, State, Team, Waker, wake_line
from lucy_api.work.quiet import NEAR_SECONDS, QUIET_TAG, QuietHours
from lucy_api.work.registry import Registry
from lucy_api.work.subscriptions import CHECKIN_CAPABILITY, CHECKIN_GRACE_SECONDS, Subscriptions
from lucy_api.work.wake import quiet_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

ACCOUNT = "acct_quiet"
LATE = datetime(2026, 10, 1, 23, 30, tzinfo=UTC)
"""Half past eleven at night, inside 23:00-07:00 on a UTC clock."""
MORNING = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)
NIGHT = "23:00-07:00@UTC"


def at(moment: datetime) -> float:
    return moment.timestamp()


# --------------------------------------------------------------------------------------
# Reading a window
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "window", ["", "23:00", "7:00-23:00", "24:00-07:00", "23:00-07:60", "07:00-07:00"]
)
def test_an_empty_malformed_or_zero_length_window_is_no_window(window: str) -> None:
    """New behaviour: only `HH:MM-HH:MM` with two different ends is quiet hours."""
    assert QuietHours.of(window, "UTC") is None


def test_a_zone_the_tz_database_does_not_have_is_no_window() -> None:
    assert QuietHours.of("23:00-07:00", "Not/AZone") is None
    assert QuietHours.from_tag("23:00-07:00@Not/AZone") is None
    assert QuietHours.from_tag("23:00-07:00") is None


def test_a_window_travels_as_a_tag_and_comes_back_the_same() -> None:
    quiet = QuietHours.of("23:00-07:00", "Europe/London")
    assert quiet is not None
    assert quiet.tag() == "23:00-07:00@Europe/London"
    assert QuietHours.from_tag(quiet.tag()) == quiet
    assert quiet.window == "23:00-07:00"


def test_a_window_that_wraps_midnight_closes_on_the_right_morning() -> None:
    """New behaviour: before midnight the window closes tomorrow; after it, today."""
    quiet = QuietHours.of("23:00-07:00", "UTC")
    assert quiet is not None
    assert quiet.ends_at(at(LATE)) == at(MORNING)
    assert quiet.ends_at(at(MORNING - timedelta(hours=4))) == at(MORNING)
    assert quiet.ends_at(at(MORNING)) is None, "the end is outside the window"
    assert quiet.ends_at(at(LATE - timedelta(hours=1))) is None


def test_a_window_inside_one_day_is_quiet_only_between_its_ends() -> None:
    quiet = QuietHours.of("13:00-14:00", "UTC")
    assert quiet is not None
    noon = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert quiet.ends_at(at(noon + timedelta(minutes=90))) == at(noon + timedelta(hours=2))
    assert quiet.ends_at(at(noon + timedelta(minutes=59))) is None
    assert quiet.ends_at(at(noon + timedelta(hours=2))) is None


def test_the_window_is_read_on_the_persons_clock_across_a_change_of_offset() -> None:
    """New behaviour: `common.timezone` decides the hours, and the end moves with the clock.

    London leaves summer time at 02:00 on 25 October 2026. A wake at 00:30 local (23:30
    UTC the day before) inside 22:00-08:00 is held until eight on the winter clock, which is
    08:00 UTC -- nine hours later, not the eight a fixed offset would give.
    """
    quiet = QuietHours.of("22:00-08:00", "Europe/London")
    assert quiet is not None
    wake = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)
    assert quiet.ends_at(at(wake)) == at(datetime(2026, 10, 25, 8, 0, tzinfo=UTC))
    assert quiet.ends_at(at(datetime(2026, 10, 24, 20, 30, tzinfo=UTC))) is None


def test_the_tool_result_says_when_a_quiet_ending_will_be_told() -> None:
    quiet = QuietHours.of("23:00-07:00", "Europe/London")
    assert quiet is not None
    assert quiet.advice() == (
        " They keep quiet hours (23:00-07:00, Europe/London): an ending inside them is told "
        "at 07:00, not when it happens."
    )


# --------------------------------------------------------------------------------------
# Which endings are quiet
# --------------------------------------------------------------------------------------


def a_record(session_id: str = "ses_1", **overrides: Any) -> Record:
    fields: dict[str, Any] = {
        "id": "wrk_watch1",
        "kind": Kind.watch,
        "role": "watch",
        "objective": "Say when CI is green",
        "session_id": session_id,
        "started_at": LATE - timedelta(minutes=4),
        "finished_at": LATE,
        "state": State.succeeded,
        "account_id": ACCOUNT,
        "wake": True,
        "tags": {QUIET_TAG: NIGHT},
    }
    return Record(**{**fields, **overrides})


def test_an_ending_is_quiet_only_inside_the_window_it_carries() -> None:
    assert quiet_until(a_record(), at(LATE)) == at(MORNING)
    assert quiet_until(a_record(tags={}), at(LATE)) is None
    assert quiet_until(a_record(), at(MORNING + timedelta(hours=1))) is None


def test_a_window_about_to_close_wakes_now_rather_than_set_a_timer_for_a_minute() -> None:
    """New behaviour: within `NEAR_SECONDS` of its end, a window holds nothing back."""
    almost = at(MORNING) - NEAR_SECONDS
    assert quiet_until(a_record(), almost) is None
    assert quiet_until(a_record(), almost - 1) == at(MORNING)


def test_a_group_is_quiet_when_any_member_carries_a_window() -> None:
    plain = a_record(id="wrk_a", tags={})
    quiet = a_record(id="wrk_b")
    assert quiet_until(Team("ses_1", "reviewers", (plain, quiet)), at(LATE)) == at(MORNING)
    assert quiet_until(Team("ses_1", "reviewers", (plain,)), at(LATE)) is None


# --------------------------------------------------------------------------------------
# The waker, the deferral, and the check-in that stands in for it
# --------------------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Any) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


class Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


class Clock:
    def __init__(self) -> None:
        self.now = LATE

    def moment(self) -> datetime:
        return self.now

    def stamp(self) -> float:
        return self.now.timestamp()


class Sleeper:
    """A sleep that waits to be let go, so a test decides when the check-in fires."""

    def __init__(self) -> None:
        self.asked: list[float] = []
        self.gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.asked.append(seconds)
        await self.gate.wait()


class Harness:
    """The registry, subscriptions and waker wired as the container wires them."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.clock = Clock()
        self.sleeper = Sleeper()
        self.registry = Registry(now=self.clock.moment)
        self.events = EventEmitter(SqlEventLog(store), Snapshot())
        self.woken = 0
        self.waker = Waker(store, self.events, wake=self.wake, clock=self.clock.stamp)
        self.registry.on_finished(self.waker.on_finished)
        self.registry.on_team_finished(self.waker.on_team_finished)
        self.subscriptions = Subscriptions(
            store,
            self.registry,
            signal_base_url="http://lucy.test/v1/signals",
            clock=self.clock.stamp,
            sleep=self.sleeper,
        )
        self.waker.attach(self.wake, defer=self.subscriptions.defer_wake)

    def wake(self) -> None:
        self.woken += 1

    async def session(self) -> str:
        created = await self.store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
        return str(created["id"])

    async def turns(self, session: str) -> list[dict[str, Any]]:
        return await self.store.records(ACCOUNT, session, "turns")

    async def items(self, session: str) -> list[dict[str, Any]]:
        return await self.store.records(ACCOUNT, session, "items")

    async def rows(self) -> list[dict[str, Any]]:
        return await self.subscriptions.open_rows()

    async def morning(self) -> None:
        self.clock.now = MORNING
        self.sleeper.gate.set()
        for _ in range(20):
            await asyncio.sleep(0.005)

    async def close(self) -> None:
        await self.subscriptions.aclose()
        await self.registry.shutdown()


async def test_a_watch_that_fires_in_quiet_hours_is_announced_now_and_told_when_they_end(
    store: SessionStore,
) -> None:
    """New behaviour: the event goes out at once; the turn waits for the window to close."""
    harness = Harness(store)
    session = await harness.session()
    record = a_record(session)

    await harness.waker.on_finished(record)

    assert await harness.turns(session) == []
    assert harness.woken == 0
    events = await store.records(ACCOUNT, session, "events")
    assert WORK_FINISHED in [str(row["type"]) for row in events]
    [row] = await harness.rows()
    assert row["capability"] == CHECKIN_CAPABILITY
    assert row["due_at"] == at(MORNING)
    assert row["grant_id"] is None
    assert str(row["objective"]).startswith(
        "Tell them what ended in their quiet hours: watch (watch) - Say when CI is green"
    )
    assert row["expires_at"] == pytest.approx(at(MORNING) + CHECKIN_GRACE_SECONDS)
    assert harness.sleeper.asked == [at(MORNING) - at(LATE)]

    await harness.morning()

    [turn] = await harness.turns(session)
    [item] = await harness.items(session)
    assert item["content"] == wake_line(record), "told as the ending itself, not the check-in"
    assert item["turn_id"] == turn["id"]
    assert harness.woken == 1
    await harness.close()


async def test_an_ending_the_person_read_in_the_meantime_is_not_told_again(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await harness.session()
    record = a_record(session)
    await harness.waker.on_finished(record)

    record.fetched = True
    await harness.morning()

    assert await harness.turns(session) == []
    assert harness.woken == 0
    await harness.close()


async def test_a_deferral_the_person_called_off_tells_nothing(store: SessionStore) -> None:
    harness = Harness(store)
    session = await harness.session()
    await harness.waker.on_finished(a_record(session))
    [standing_in] = harness.registry.running(session)

    harness.registry.cancel(standing_in.id)
    for _ in range(20):
        await asyncio.sleep(0.005)

    assert await harness.turns(session) == []
    assert harness.woken == 0
    await harness.close()


async def test_a_group_that_ends_in_quiet_hours_is_deferred_as_one_line(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    session = await harness.session()
    team = Team(session, "reviewers", (a_record(session, id="wrk_a", group="reviewers"),))

    await harness.waker.on_team_finished(team)

    [row] = await harness.rows()
    assert "group reviewers (1 member) has ended" in str(row["objective"])
    assert row["grant_id"] is None
    await harness.close()


async def test_a_deferral_carries_the_endings_consent_for_what_is_left_of_it(
    store: SessionStore,
) -> None:
    """New behaviour: the morning turn may still act, if the consent outlived the night."""
    harness = Harness(store)
    session = await harness.session()
    record = a_record(session, kind=Kind.subscription, tags={QUIET_TAG: NIGHT, "grant": "dgt_1"})

    standing_in = await harness.subscriptions.defer_wake(record, at(MORNING))

    [row] = await harness.rows()
    assert row["grant_id"] == "dgt_1"
    assert row["work_id"] == standing_in
    await harness.close()


async def test_a_deferral_for_a_session_that_is_gone_is_not_recorded(
    store: SessionStore,
) -> None:
    harness = Harness(store)
    assert await harness.subscriptions.defer_wake(a_record("ses_gone"), at(MORNING)) is None
    assert await harness.rows() == []
    await harness.close()


async def test_a_deferral_that_cannot_be_recorded_wakes_now_rather_than_never(
    store: SessionStore,
) -> None:
    """New behaviour: a notice is never lost to quiet hours; at worst it is on time."""
    harness = Harness(store)

    async def refuse(ending: Any, due_at: float) -> str | None:
        return None

    harness.waker.attach(harness.wake, defer=refuse)
    session = await harness.session()

    await harness.waker.on_finished(a_record(session))

    assert len(await harness.turns(session)) == 1
    assert harness.woken == 1
    await harness.close()


async def test_a_waker_with_nothing_to_defer_to_wakes_now(store: SessionStore) -> None:
    harness = Harness(store)
    harness.waker = Waker(store, harness.events, wake=harness.wake, clock=harness.clock.stamp)
    session = await harness.session()

    await harness.waker.on_finished(a_record(session))

    assert len(await harness.turns(session)) == 1
    await harness.close()


async def test_a_wake_held_for_a_running_turn_is_still_deferred_when_it_is_spent(
    store: SessionStore,
) -> None:
    """New behaviour: the end of a turn at midnight does not open a second one at midnight."""
    harness = Harness(store)
    session = await harness.session()
    record = a_record(session)
    harness.waker._held[session] = [record]

    await harness.waker.flush(session)

    assert await harness.turns(session) == []
    [row] = await harness.rows()
    assert row["due_at"] == at(MORNING)
    await harness.close()


async def test_what_a_turn_stamped_on_a_subscription_survives_a_restart(
    store: SessionStore,
) -> None:
    """New behaviour: the window and the withheld consent are on the row, not only in memory."""
    harness = Harness(store)
    session = await harness.session()
    await harness.subscriptions.open(
        account_id=ACCOUNT,
        session_id=session,
        profile="personal",
        capability="repos",
        objective="Say when CI is green",
        timeout_seconds=3600,
        tags={QUIET_TAG: NIGHT, "consent": "withheld", "capability": "forged"},
    )
    await harness.close()

    after = Harness(store)
    assert await after.subscriptions.restore() == 1
    [record] = after.registry.running(session)
    assert record.tags[QUIET_TAG] == NIGHT
    assert record.tags["consent"] == "withheld"
    assert record.tags["capability"] == "repos", "a passed tag never stands in for the hub's"
    await after.close()
