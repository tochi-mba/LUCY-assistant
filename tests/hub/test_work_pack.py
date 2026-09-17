"""The capability that answers "what is still going?", as the model actually calls it.

These drive the real weftai runtime over the real operations, because what is worth pinning
here is what a direct call to a handler would miss: that `work.cancel` is a write and is
therefore refused in a read-only turn, and that every refusal comes back as a *result* the
model can act on rather than an exception it can only apologise for.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from lucy_api.packs.base import State as PackState
from lucy_api.packs.context import PackContext, SilentTokens
from lucy_api.packs.http import NullHttp
from lucy_api.packs.registry import build_registry, build_runtime
from lucy_api.packs.work import WorkPack
from lucy_api.work import Brief, Kind, Registry

SESSION = "ses_1"
START = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def a_context(registry: Registry | None) -> PackContext:
    return PackContext(
        account_id="acc_1",
        profile="personal",
        session_id=SESSION,
        http=NullHttp(),
        tokens=SilentTokens(),
        work=registry,
    )


def a_brief(**overrides: Any) -> Brief:
    fields: dict[str, Any] = {
        "session_id": SESSION,
        "kind": Kind.helper,
        "role": "researcher",
        "objective": "Find out when the tour reaches Europe",
    }
    return Brief(**{**fields, **overrides})


async def settled() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


async def run(plan: dict[str, Any], context: PackContext, *, writes: bool = False) -> Any:
    operations = WorkPack().operations(context)
    runtime = build_runtime(build_registry(operations))
    return await runtime.execute(plan, {"ctx": context, "allowWrites": writes})


def step(result: Any, index: int = 0) -> Any:
    return result["steps"][index]["data"]


# --------------------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------------------


async def test_it_is_ready_even_when_everything_it_reports_on_is_broken() -> None:
    """This is the capability a model reaches for *because* something else went wrong."""
    pack = WorkPack()
    registry = Registry(now=Clock())

    available = await pack.probe(a_context(registry))

    assert available.state is PackState.ready
    assert available.detail == "nothing running"


async def test_the_state_line_counts_what_is_running() -> None:
    registry = Registry(now=Clock())
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    assert (await WorkPack().probe(a_context(registry))).detail == "1 running"

    registry.cancel(handle.id)
    await settled()


async def test_a_turn_with_no_registry_offers_nothing_rather_than_failing_later() -> None:
    """An operation that cannot work is worse than one that is not there."""
    pack = WorkPack()
    context = a_context(None)

    assert pack.operations(context) == ()
    assert (await pack.probe(context)).state is PackState.not_configured


# --------------------------------------------------------------------------------------
# Listing and checking in
# --------------------------------------------------------------------------------------


async def test_helpers_and_jobs_come_back_in_one_list() -> None:
    """One list, because from where the model is sitting it is one question."""
    clock = Clock()
    registry = Registry(now=clock)
    helper = registry.start(asyncio.Event().wait(), a_brief())
    job = registry.start(
        asyncio.Event().wait(),
        a_brief(kind=Kind.job, role="download", objective="Fetch the recording"),
    )
    await settled()

    listed = step(await run({"steps": [{"id": "now", "op": "work.list"}]}, a_context(registry)))

    assert listed["count"] == 2
    assert {entry["kind"] for entry in listed["running"]} == {"helper", "job"}
    assert {entry["id"] for entry in listed["running"]} == {helper.id, job.id}

    registry.cancel(helper.id)
    registry.cancel(job.id)
    await settled()


async def test_an_empty_list_says_so_rather_than_returning_a_bare_zero() -> None:
    registry = Registry(now=Clock())

    listed = step(await run({"steps": [{"id": "now", "op": "work.list"}]}, a_context(registry)))

    assert listed["count"] == 0
    assert listed["advice"] == "Nothing is running."


async def test_checking_in_reports_a_completion_once_and_never_the_result() -> None:
    registry = Registry(now=Clock())

    async def enormous() -> str:
        return "x" * 40_000

    registry.start(enormous(), a_brief(kind=Kind.job, role="download"))
    await settled()
    context = a_context(registry)

    first = step(await run({"steps": [{"id": "news", "op": "work.check"}]}, context))
    assert first["count"] == 1
    assert first["finished"][0]["result_tokens"] == 10_000
    assert "x" * 100 not in str(first), "the notice describes the answer, it is not the answer"

    second = step(await run({"steps": [{"id": "news", "op": "work.check"}]}, context))
    assert second["count"] == 0
    assert "Nothing has finished" in second["advice"]


# --------------------------------------------------------------------------------------
# Reading a result
# --------------------------------------------------------------------------------------


async def test_a_result_is_read_deliberately_and_arrives_whole() -> None:
    registry = Registry(now=Clock())

    async def finds() -> dict[str, list[str]]:
        return {"dates": ["12 May", "14 May"]}

    handle = registry.start(finds(), a_brief())
    await settled()

    read = step(
        await run(
            {"steps": [{"id": "read", "op": "work.result", "input": {"work_id": handle.id}}]},
            a_context(registry),
        )
    )

    assert read["state"] == "succeeded"
    assert read["payload"] == {"dates": ["12 May", "14 May"]}


async def test_reading_too_early_comes_back_as_advice_not_as_a_crash() -> None:
    """A model handed a stack trace apologises. A model handed a sentence carries on."""
    registry = Registry(now=Clock())
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    result = await run(
        {"steps": [{"id": "read", "op": "work.result", "input": {"work_id": handle.id}}]},
        a_context(registry),
    )

    assert result["ok"] is True, "the step succeeded; it is the work that has not"
    assert step(result)["status"] == "still_running"
    assert "rather than polling" in step(result)["message"]

    registry.cancel(handle.id)
    await settled()


async def test_an_invented_id_is_named_as_invented() -> None:
    registry = Registry(now=Clock())

    result = step(
        await run(
            {"steps": [{"id": "read", "op": "work.result", "input": {"work_id": "wrk_no"}}]},
            a_context(registry),
        )
    )

    assert result["status"] == "unknown"
    assert "wrk_no" in result["message"]


# --------------------------------------------------------------------------------------
# Waiting
# --------------------------------------------------------------------------------------


async def test_waiting_gives_up_without_stopping_the_work() -> None:
    registry = Registry(now=Clock())
    release = asyncio.Event()

    async def eventually() -> str:
        await release.wait()
        return "landed"

    handle = registry.start(eventually(), a_brief())
    await settled()
    context = a_context(registry)

    gave_up = step(
        await run(
            {
                "steps": [
                    {
                        "id": "hold",
                        "op": "work.wait",
                        "input": {"work_id": handle.id, "seconds": 0.01},
                    }
                ]
            },
            context,
        )
    )
    assert gave_up["status"] == "still_running"
    assert "has not been stopped" in gave_up["message"]

    release.set()
    await settled()

    landed = step(
        await run(
            {"steps": [{"id": "hold", "op": "work.wait", "input": {"work_id": handle.id}}]},
            context,
        )
    )
    assert landed["state"] == "succeeded"
    assert landed["advice"] == "It finished. Read it with work.result."


async def test_waiting_longer_than_the_ceiling_waits_the_ceiling() -> None:
    """A turn nobody is watching any more is not a turn worth holding open."""
    registry = Registry(now=Clock())
    asked: list[float] = []

    async def quick() -> str:
        return "done"

    handle = registry.start(quick(), a_brief())
    await settled()

    real_wait = registry.wait

    async def record(work_id: str, seconds: float) -> Any:
        asked.append(seconds)
        return await real_wait(work_id, seconds)

    registry.wait = record  # type: ignore[method-assign]

    await run(
        {
            "steps": [
                {
                    "id": "hold",
                    "op": "work.wait",
                    "input": {"work_id": handle.id, "seconds": 9_000},
                }
            ]
        },
        a_context(registry),
    )

    assert asked == [120.0]


async def test_waiting_on_an_invented_id_says_so() -> None:
    registry = Registry(now=Clock())

    result = step(
        await run(
            {"steps": [{"id": "hold", "op": "work.wait", "input": {"work_id": "wrk_no"}}]},
            a_context(registry),
        )
    )

    assert result["status"] == "unknown"


# --------------------------------------------------------------------------------------
# Stopping
# --------------------------------------------------------------------------------------


async def test_cancelling_is_a_write_and_a_read_only_turn_refuses_the_whole_plan() -> None:
    """Plan mode is trustworthy only because this is the default rather than a reminder."""
    registry = Registry(now=Clock())
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    result = await run(
        {"steps": [{"id": "stop", "op": "work.cancel", "input": {"work_id": handle.id}}]},
        a_context(registry),
    )

    assert result["ok"] is False
    assert result["steps"] == [], "refused before anything ran, not part-way through"
    assert result["issues"][0]["code"] == "step.write_not_allowed"
    assert not registry.running(SESSION)[0].cancel_requested

    registry.cancel(handle.id)
    await settled()


async def test_cancelling_says_it_asked_rather_than_claiming_it_stopped() -> None:
    registry = Registry(now=Clock())
    handle = registry.start(asyncio.Event().wait(), a_brief())
    await settled()

    stopped = step(
        await run(
            {"steps": [{"id": "stop", "op": "work.cancel", "input": {"work_id": handle.id}}]},
            a_context(registry),
            writes=True,
        )
    )

    assert stopped["advice"] == "Asked it to stop. Its notice will say so."
    await settled()
    assert registry.result(handle.id).state.value == "cancelled"


async def test_cancelling_something_already_finished_says_that_instead() -> None:
    registry = Registry(now=Clock())

    async def quick() -> str:
        return "done"

    handle = registry.start(quick(), a_brief())
    await settled()

    stopped = step(
        await run(
            {"steps": [{"id": "stop", "op": "work.cancel", "input": {"work_id": handle.id}}]},
            a_context(registry),
            writes=True,
        )
    )

    assert stopped["state"] == "succeeded"
    assert "nothing was stopped" in stopped["advice"]


async def test_cancelling_an_invented_id_says_so() -> None:
    registry = Registry(now=Clock())

    result = step(
        await run(
            {"steps": [{"id": "stop", "op": "work.cancel", "input": {"work_id": "wrk_no"}}]},
            a_context(registry),
            writes=True,
        )
    )

    assert result["status"] == "unknown"


# --------------------------------------------------------------------------------------
# What the pack declares
# --------------------------------------------------------------------------------------


def test_stopping_something_is_a_permission_a_person_can_reason_about() -> None:
    """Nobody wants to approve five operations. Everybody understands "stop something"."""
    (permission,) = WorkPack().permissions()

    assert permission.id == "work.stop"
    assert permission.covers == ("work.cancel",)
    assert permission.risk == "write"


def test_it_has_no_setup_because_there_is_nothing_to_connect() -> None:
    assert WorkPack().setup() is None
    assert WorkPack().docs is None
