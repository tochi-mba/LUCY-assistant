"""What the registry says to the rest of the hub when work ends, and what it says about watches.

Split from the registry's own tests because these are the seams the wake and the stream
hang off: a listener per ending, delivered after the record is final, never able to break
the ending it is about.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lucy_api.work import Brief, Kind, Record, Registry, State, UnknownWorkError, WorkError, new_id

SESSION = "ses_1"
START = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


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
        "objective": "Check the migration",
    }
    return Brief(**{**fields, **overrides})


async def settled() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


async def quick() -> str:
    return "done"


# --------------------------------------------------------------------------------------
# Listeners
# --------------------------------------------------------------------------------------


async def test_every_listener_hears_every_ending_after_it_is_final() -> None:
    registry = Registry(now=Clock())
    heard: list[tuple[str, State, bool]] = []

    async def listener(record: Record) -> None:
        heard.append((record.id, record.state, record.finished_at is not None))

    registry.on_finished(listener)
    registry.on_finished(listener)
    handle = registry.start(quick(), a_brief())
    await settled()

    assert heard == [(handle.id, State.succeeded, True)] * 2


async def test_a_listener_that_raises_is_logged_with_the_type_and_stops_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = Registry(now=Clock())
    after: list[str] = []

    async def broken(record: Record) -> None:
        message = f"payload was {record.payload}"
        raise RuntimeError(message)

    async def fine(record: Record) -> None:
        after.append(record.id)

    registry.on_finished(broken)
    registry.on_finished(fine)
    with caplog.at_level(logging.WARNING, logger="lucy_api.work.registry"):
        handle = registry.start(quick(), a_brief())
        await settled()

    assert after == [handle.id]
    assert registry.result(handle.id).state is State.succeeded
    warning = next(rec for rec in caplog.records if rec.getMessage() == "work listener failed")
    assert warning.error == "RuntimeError"
    assert "done" not in warning.getMessage()


async def test_shutdown_waits_for_deliveries_so_no_ending_goes_unannounced() -> None:
    registry = Registry(now=Clock())
    gate = asyncio.Event()
    heard: list[State] = []

    async def slow_listener(record: Record) -> None:
        await gate.wait()
        heard.append(record.state)

    registry.on_finished(slow_listener)
    registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    closing = asyncio.create_task(registry.shutdown())
    await settled()
    assert heard == [], "the listener is still being awaited"
    gate.set()
    await closing

    assert heard == [State.cancelled]


# --------------------------------------------------------------------------------------
# Endings written for the model
# --------------------------------------------------------------------------------------


async def test_an_ending_that_knows_its_own_sentence_reaches_the_notice_in_words() -> None:
    registry = Registry(now=Clock())

    async def explains() -> str:
        message = "5 checks in a row failed (ConnectionError)"
        raise WorkError(message)

    registry.start(explains(), a_brief())
    await settled()

    notice = registry.drain(SESSION)[0]
    assert notice.state is State.failed
    assert notice.detail == "5 checks in a row failed (ConnectionError)"


async def test_a_watch_that_times_out_is_told_it_expired_and_offered_again() -> None:
    registry = Registry(now=Clock())

    async def never() -> str:
        await asyncio.sleep(10)
        return "late"

    registry.start(never(), a_brief(kind=Kind.watch, role="watch", timeout_seconds=0.01))
    await asyncio.sleep(0.05)

    notice = registry.drain(SESSION)[0]
    assert notice.state is State.timed_out
    assert notice.detail == "expired after 0s without firing; start it again if you still need it"


async def test_a_helper_that_times_out_is_still_told_it_may_be_running() -> None:
    registry = Registry(now=Clock())

    async def never() -> str:
        await asyncio.sleep(10)
        return "late"

    registry.start(never(), a_brief(timeout_seconds=0.01))
    await asyncio.sleep(0.05)

    assert "may still be running" in registry.drain(SESSION)[0].detail


# --------------------------------------------------------------------------------------
# Wake, account, and the small reads
# --------------------------------------------------------------------------------------


async def test_a_wake_without_an_account_is_not_honoured_because_it_cannot_be() -> None:
    registry = Registry(now=Clock())

    registry.start(asyncio.Event().wait(), a_brief(wake=True))
    registry.start(asyncio.Event().wait(), a_brief(wake=True, account_id="acc_1"))
    await settled()

    orphan, owned = registry.running(SESSION)
    assert (orphan.wake, orphan.account_id) == (False, "")
    assert (owned.wake, owned.account_id) == (True, "acc_1")
    for record in (orphan, owned):
        registry.cancel(record.id)
    await settled()


async def test_state_of_answers_without_marking_anything_delivered() -> None:
    registry = Registry(now=Clock())
    handle = registry.start(quick(), a_brief())
    await settled()

    assert registry.state_of(handle.id) is State.succeeded
    assert registry.drain(SESSION)[0].id == handle.id, "the notice was still undelivered"
    with pytest.raises(UnknownWorkError):
        registry.state_of("wrk_nobody")


def test_new_ids_are_handles_nobody_can_guess() -> None:
    first, second = new_id(), new_id()
    assert first.startswith("wrk_")
    assert first != second
    assert len(first) > 12


def test_a_record_renders_its_own_notice() -> None:
    record = Record(
        id="wrk_1",
        kind=Kind.watch,
        role="watch",
        objective="Say when it lands",
        session_id=SESSION,
        started_at=START,
        finished_at=START + timedelta(seconds=9),
        state=State.succeeded,
        tokens=3,
        detail="",
    )

    notice = record.notice(START + timedelta(hours=1))

    assert notice.elapsed_seconds == 9
    assert notice.line() == (
        "watch (watch) - Say when it lands - succeeded - "
        "about 3 tokens of result, fetch it to read it"
    )
