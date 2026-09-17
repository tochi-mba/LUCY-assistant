"""One shape for a helper, a long job and a shell command.

The properties being asserted here are mostly about what *cannot* happen: a completion
cannot be announced twice, a result cannot arrive uninvited, a cancellation cannot be
mistaken for a timeout, and nothing can wait forever. Each of those is a bug that only shows
up an hour into a session, which is exactly why they are pinned here.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from lucy_api.work import (
    AtCapacityError,
    Brief,
    Kind,
    Registry,
    State,
    StillRunningError,
    UnknownWorkError,
    notices_block,
)

SESSION = "ses_1"
START = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class Clock:
    """A clock that only moves when a test moves it.

    Elapsed time is rendered into a line the model reads, so it has to be asserted exactly.
    A real clock would make "0s" and "1s" a coin toss on a slow machine.
    """

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def a_registry(**overrides: Any) -> tuple[Registry, Clock]:
    clock = Clock()
    return Registry(now=clock, **overrides), clock


def a_brief(**overrides: Any) -> Brief:
    fields: dict[str, Any] = {
        "session_id": SESSION,
        "kind": Kind.helper,
        "role": "reviewer",
        "objective": "Check the migration for anything that cannot be undone",
    }
    return Brief(**{**fields, **overrides})


async def settled() -> None:
    """Let every scheduled task reach its next await, without sleeping for real."""
    for _ in range(4):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------------------
# Starting
# --------------------------------------------------------------------------------------


async def test_starting_returns_a_handle_before_the_work_is_anywhere_near_done() -> None:
    """The step completes; the work does not. Everything else depends on this being true."""
    registry, _ = a_registry()
    started = asyncio.Event()

    async def slow() -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    handle = registry.start(slow(), a_brief())

    assert handle.id.startswith("wrk_")
    assert handle.objective.startswith("Check the migration")
    assert not started.is_set(), "start() scheduled the work rather than running it"

    await settled()
    assert started.is_set(), "and the event loop then picked it up without anybody awaiting"

    registry.cancel(handle.id)
    await settled()


async def test_a_long_objective_is_cut_where_a_person_can_see_it_was_cut() -> None:
    registry, _ = a_registry()

    async def quick() -> str:
        return "done"

    handle = registry.start(quick(), a_brief(objective="word " * 200, role="x" * 200))
    await settled()

    assert handle.objective.endswith("…")
    assert len(handle.objective) <= 160
    assert len(handle.role) <= 40


async def test_the_cap_refuses_rather_than_queueing_so_the_model_can_decide() -> None:
    """A twenty-first thing silently waiting behind twenty is worse than being told."""
    registry, _ = a_registry(max_concurrent=2)
    forever = [asyncio.Event().wait() for _ in range(2)]
    handles = [registry.start(work, a_brief()) for work in forever]
    await settled()

    refused = asyncio.Event().wait()
    with pytest.raises(AtCapacityError) as raised:
        registry.start(refused, a_brief())

    with pytest.raises(RuntimeError, match="cannot reuse"):
        await refused  # the refusal closed it, rather than leaving it to warn later

    assert "2 things are already running" in str(raised.value)
    assert "do this inline" in str(raised.value), "the message says what to do instead"

    for handle in handles:
        registry.cancel(handle.id)
    await settled()


async def test_a_refusal_leaves_a_task_alone_because_the_loop_already_owns_it() -> None:
    """Closing a coroutine is right; closing something the loop is running is not."""
    registry, _ = a_registry(max_concurrent=1)
    running = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    already_scheduled = asyncio.ensure_future(asyncio.sleep(0))
    with pytest.raises(AtCapacityError):
        registry.start(already_scheduled, a_brief())

    await already_scheduled
    assert already_scheduled.done()
    assert not already_scheduled.cancelled()

    registry.cancel(running.id)
    await settled()


async def test_another_sessions_work_does_not_count_against_this_ones_cap() -> None:
    registry, _ = a_registry(max_concurrent=1)
    elsewhere = registry.start(asyncio.Event().wait(), a_brief(session_id="ses_2"))
    await settled()

    here = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    assert len(registry.running(SESSION)) == 1
    assert len(registry.running("ses_2")) == 1

    registry.cancel(elsewhere.id)
    registry.cancel(here.id)
    await settled()


# --------------------------------------------------------------------------------------
# Finishing, and how the model hears about it
# --------------------------------------------------------------------------------------


async def test_a_completion_is_announced_once_and_then_never_again() -> None:
    """Twice is worse than never: the model acts on the second one as if it were news."""
    registry, clock = a_registry()

    async def quick() -> str:
        return "the answer"

    handle = registry.start(quick(), a_brief())
    clock.advance(3)
    await settled()

    first = registry.drain(SESSION)
    assert [notice.id for notice in first] == [handle.id]
    assert first[0].state is State.succeeded
    assert first[0].elapsed_seconds == 3

    assert registry.drain(SESSION) == (), "the second drain has nothing left to say"


async def test_a_notice_says_how_big_the_answer_is_and_never_carries_it() -> None:
    """A job that produced forty megabytes of log must not arrive uninvited."""
    registry, _ = a_registry()

    async def enormous() -> str:
        return "x" * 40_000

    handle = registry.start(enormous(), a_brief(kind=Kind.job, role="download"))
    await settled()

    notice = registry.drain(SESSION)[0]

    assert notice.tokens == 10_000
    assert "x" * 100 not in notice.line(), "the answer itself is not in the notice"
    assert "fetch it to read it" in notice.line()
    assert notice.id == handle.id


async def test_a_failure_carries_the_error_type_and_not_the_error_body() -> None:
    """A third-party error routinely carries the response that caused it."""
    registry, _ = a_registry()

    async def explodes() -> str:
        raise RuntimeError("secret=hunter2 in the response body")

    registry.start(explodes(), a_brief())
    await settled()

    notice = registry.drain(SESSION)[0]

    assert notice.state is State.failed
    assert notice.detail == "RuntimeError"
    assert "hunter2" not in notice.line()


async def test_a_timeout_is_told_apart_from_a_failure_because_they_differ() -> None:
    """ "It failed" when the truth is "we stopped waiting" is a false statement."""
    registry, _ = a_registry()

    async def slower_than_allowed() -> str:
        await asyncio.sleep(10)
        return "late"

    registry.start(slower_than_allowed(), a_brief(timeout_seconds=0.01))
    await asyncio.sleep(0.05)

    notice = registry.drain(SESSION)[0]

    assert notice.state is State.timed_out
    assert "may still be running" in notice.detail


async def test_work_with_no_timeout_runs_until_it_is_done() -> None:
    """Some things are legitimately unbounded, and a zero says so rather than meaning zero."""
    registry, _ = a_registry()

    async def quick() -> str:
        return "done"

    registry.start(quick(), a_brief(timeout_seconds=0))
    await settled()

    assert registry.drain(SESSION)[0].state is State.succeeded


async def test_nothing_finishes_twice_however_it_ended() -> None:
    registry, clock = a_registry()

    async def quick() -> str:
        return "done"

    handle = registry.start(quick(), a_brief())
    await settled()
    first = registry.result(handle.id)

    clock.advance(60)
    registry.cancel(handle.id)

    assert registry.result(handle.id).state is first.state
    assert registry.result(handle.id).payload == "done"


# --------------------------------------------------------------------------------------
# The live block
# --------------------------------------------------------------------------------------


async def test_the_live_block_lists_helpers_and_jobs_together_as_one_question() -> None:
    """From where the model sits it is one question: what is still in flight?"""
    registry, clock = a_registry()
    helper = registry.start(asyncio.Event().wait(), a_brief())
    job = registry.start(
        asyncio.Event().wait(),
        a_brief(kind=Kind.job, role="download", objective="Fetch the recording"),
    )
    await settled()
    clock.advance(74)

    shown = registry.snapshot(SESSION)

    assert {work.kind for work in shown} == {"helper", "job"}
    assert all(work.status == "running" for work in shown)
    assert all(work.elapsed_seconds == 74 for work in shown)
    assert {work.id for work in shown} == {helper.id, job.id}

    registry.cancel(helper.id)
    registry.cancel(job.id)
    await settled()


async def test_a_completion_shows_as_news_for_exactly_one_turn() -> None:
    """The delta is what drives the next move; a stale delta is a lie about what changed."""
    registry, _ = a_registry()

    async def quick() -> str:
        return "done"

    registry.start(quick(), a_brief())
    await settled()

    peeked = registry.snapshot(SESSION)
    assert peeked[0].finished_since_last_turn is True

    announced = registry.snapshot(SESSION, announce=True)
    assert announced[0].finished_since_last_turn is True
    assert announced[0].status == "succeeded"

    assert registry.snapshot(SESSION) == (), "once shown, it is no longer news"


async def test_the_last_thing_a_run_said_about_itself_is_what_is_shown() -> None:
    """One line, overwritten. A history of progress notes is the transcript this avoids."""
    registry, _ = a_registry()
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    registry.progress(handle.id, "read 12 of 40 files")
    registry.progress(handle.id, "read 31 of 40 files")

    assert registry.snapshot(SESSION)[0].progress == "read 31 of 40 files"

    registry.cancel(handle.id)
    await settled()


async def test_a_finished_run_falls_back_to_its_own_ending_for_its_line() -> None:
    registry, _ = a_registry()

    async def explodes() -> str:
        raise ValueError

    registry.start(explodes(), a_brief())
    await settled()

    assert registry.snapshot(SESSION)[0].progress == "ValueError"


# --------------------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------------------


async def test_a_result_is_fetched_on_purpose_and_never_pushed() -> None:
    registry, _ = a_registry()

    async def quick() -> dict[str, int]:
        return {"found": 3}

    handle = registry.start(quick(), a_brief())
    await settled()

    result = registry.result(handle.id)

    assert result.state is State.succeeded
    assert result.payload == {"found": 3}


async def test_fetching_too_early_says_to_wait_rather_than_to_poll() -> None:
    registry, _ = a_registry()
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    with pytest.raises(StillRunningError) as raised:
        registry.result(handle.id)

    assert "rather than polling" in str(raised.value)

    registry.cancel(handle.id)
    await settled()


async def test_an_invented_handle_is_named_as_invented() -> None:
    """A model that asked about an id it made up will ask again unless it is told."""
    registry, _ = a_registry()

    with pytest.raises(UnknownWorkError) as raised:
        registry.result("wrk_nope")

    assert "wrk_nope" in str(raised.value)


async def test_waiting_has_a_deadline_and_the_deadline_does_not_stop_the_work() -> None:
    """ "Is it done yet?" is answered with "not yet", and the download carries on."""
    registry, _ = a_registry()
    release = asyncio.Event()

    async def eventually() -> str:
        await release.wait()
        return "landed"

    handle = registry.start(eventually(), a_brief())
    await settled()

    with pytest.raises(StillRunningError) as raised:
        await registry.wait(handle.id, 0.01)
    assert "has not been stopped" in str(raised.value)

    release.set()
    result = await registry.wait(handle.id, 1)
    assert result.payload == "landed"


async def test_waiting_on_something_already_finished_answers_at_once() -> None:
    registry, _ = a_registry()

    async def quick() -> str:
        return "done"

    handle = registry.start(quick(), a_brief())
    await settled()
    registry.drain(SESSION)

    assert (await registry.wait(handle.id, 0)).payload == "done"


# --------------------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------------------


async def test_cancelling_twice_is_cancelling_once() -> None:
    """Somebody pressing stop twice must not be able to produce an error."""
    registry, _ = a_registry()
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    registry.cancel(handle.id)
    await settled()
    again = registry.cancel(handle.id)

    assert again.state is State.cancelled
    assert again.cancel_requested is True
    assert registry.result(handle.id).state is State.cancelled


async def test_cancelling_something_that_already_finished_leaves_its_ending_alone() -> None:
    registry, _ = a_registry()

    async def quick() -> str:
        return "done"

    handle = registry.start(quick(), a_brief())
    await settled()

    assert registry.cancel(handle.id).state is State.succeeded


async def test_shutdown_leaves_nothing_running_and_nothing_without_an_ending() -> None:
    """A process going down must not leave work whose final state was never recorded."""
    registry, _ = a_registry()
    handles = [registry.start(asyncio.Event().wait(), a_brief()) for _ in range(3)]
    await settled()

    await registry.shutdown()

    assert registry.running(SESSION) == ()
    assert all(registry.result(handle.id).state is State.cancelled for handle in handles)


async def test_shutdown_waits_out_work_that_fails_on_its_way_down() -> None:
    registry, _ = a_registry()

    async def resists() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup went wrong") from None
        return "never"

    registry.start(resists(), a_brief())
    await settled()

    await registry.shutdown()

    assert registry.running(SESSION) == ()


# --------------------------------------------------------------------------------------
# Not growing without bound
# --------------------------------------------------------------------------------------


async def test_old_finished_work_is_forgotten_once_it_has_been_reported() -> None:
    registry, clock = a_registry(keep_finished=2)

    async def quick() -> str:
        return "done"

    kept: list[str] = []
    for _ in range(5):
        handle = registry.start(quick(), a_brief())
        await settled()
        clock.advance(1)
        registry.drain(SESSION)
        kept.append(handle.id)

    surviving = [work_id for work_id in kept if _exists(registry, work_id)]
    assert surviving == kept[-2:], "the oldest reported completions went, the newest stayed"


async def test_a_completion_nobody_has_been_told_about_is_never_forgotten() -> None:
    """Forgetting one is how a model waits forever for something that ended an hour ago."""
    registry, clock = a_registry(keep_finished=1)

    async def quick() -> str:
        return "done"

    handles = []
    for _ in range(4):
        handles.append(registry.start(quick(), a_brief()))
        await settled()
        clock.advance(1)

    assert all(_exists(registry, handle.id) for handle in handles)
    assert len(registry.drain(SESSION)) == 4


def _exists(registry: Registry, work_id: str) -> bool:
    try:
        registry.result(work_id)
    except UnknownWorkError:
        return False
    return True


# --------------------------------------------------------------------------------------
# The paragraph the model reads
# --------------------------------------------------------------------------------------


async def test_nothing_to_report_renders_nothing_at_all() -> None:
    """A heading over no lines teaches the model to skim past the heading."""
    assert notices_block(()) == ""


async def test_the_block_says_plainly_that_it_is_not_the_result() -> None:
    registry, clock = a_registry()

    async def quick() -> str:
        return "a short answer"

    registry.start(quick(), a_brief(role="researcher", objective="Look up the dates"))
    clock.advance(12)
    await settled()

    block = notices_block(registry.drain(SESSION))

    assert "Nothing here is the result itself" in block
    assert "researcher (helper) - Look up the dates - succeeded" in block
