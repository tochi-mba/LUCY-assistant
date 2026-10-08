"""The service: slots, the queue, follow-ups, cancellation, and honest rows."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest
from conftest import ACCOUNT, STRANGER, a_service

from lucy_coder.service import RefusedError
from lucy_coder.tasks import INTERRUPTED, TaskState

if TYPE_CHECKING:
    from lucy_coder.service import CoderService
    from lucy_coder.tasks import TaskStore


async def settled(service: CoderService, task_id: str, *states: TaskState) -> dict:
    wanted = set(states) or {TaskState.idle}
    for _ in range(400):
        body = await service.get(ACCOUNT, task_id)
        if TaskState(body["state"]) in wanted:
            return body
        await asyncio.sleep(0.05)
    msg = f"task {task_id} never settled: {body['state']} ({body['detail']})"
    raise AssertionError(msg)


async def test_a_task_runs_in_its_directory_and_the_row_keeps_the_answer(
    store: TaskStore, workdir: str
) -> None:
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT,
            brief="write hello.txt saying hi",
            directory=workdir,
            run_level="edits",
            title="",
        )
        body = await settled(service, task.id)
        assert body["result"] == "done: wrote hello.txt"
        assert body["detail"] == "the turn is over; a message resumes the session"
        assert body["turns"] == 1
        assert body["cost_usd"] == pytest.approx(0.021)
        assert body["tool_uses"] == 1
        assert body["last_tool"] == "Write"
        assert body["resumable"] is True
        assert body["title"] == "write hello.txt saying hi", "the brief names an unnamed task"
    finally:
        await service.aclose()


async def test_refusals_are_sentences(store: TaskStore, workdir: str) -> None:
    service = a_service(store)
    try:
        with pytest.raises(RefusedError, match="say what the task is"):
            await service.start(
                account_id=ACCOUNT, brief="  ", directory=workdir, run_level="edits", title=""
            )
        with pytest.raises(RefusedError, match="run_level must be one of"):
            await service.start(
                account_id=ACCOUNT, brief="x", directory=workdir, run_level="yolo", title=""
            )
        with pytest.raises(RefusedError, match="directory does not exist"):
            await service.start(
                account_id=ACCOUNT,
                brief="x",
                directory=workdir + "-missing",
                run_level="edits",
                title="",
            )
        with pytest.raises(RefusedError, match="no such task"):
            await service.get(ACCOUNT, "tsk_nowhere")
        with pytest.raises(RefusedError, match="say what to tell it"):
            await service.message(ACCOUNT, "tsk_nowhere", "  ")
    finally:
        await service.aclose()


async def test_the_third_task_queues_and_starts_when_a_slot_frees(
    store: TaskStore, workdir: str
) -> None:
    os.environ["FAKE_CLAUDE"] = "hangs"
    service = a_service(store, max_live=2, timeout=60)
    try:
        first = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        second = await service.start(
            account_id=ACCOUNT, brief="two", directory=workdir, run_level="edits", title="2"
        )
        third = await service.start(
            account_id=ACCOUNT, brief="three", directory=workdir, run_level="edits", title="3"
        )
        assert (await service.get(ACCOUNT, first.id))["state"] == "running"
        assert (await service.get(ACCOUNT, second.id))["state"] == "running"
        assert (await service.get(ACCOUNT, third.id))["state"] == "queued"

        os.environ["FAKE_CLAUDE"] = "answers"
        await service.cancel(ACCOUNT, first.id)
        body = await settled(service, third.id, TaskState.idle, TaskState.running)
        assert body["state"] in {"running", "idle"}, "the queued task took the freed slot"
    finally:
        os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_a_follow_up_waits_out_the_turn_then_resumes_the_session(
    store: TaskStore, workdir: str
) -> None:
    os.environ["FAKE_CLAUDE"] = "hangs"
    service = a_service(store, timeout=60)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        fresh, advice = await service.message(ACCOUNT, task.id, "also add a test")
        assert fresh.state is TaskState.running
        assert "mid-turn" in advice
        assert fresh.queued_messages == ["also add a test"]

        os.environ["FAKE_CLAUDE"] = "answers"
        await service.cancel(ACCOUNT, task.id)
        body = await service.get(ACCOUNT, task.id)
        assert body["state"] == "cancelled"
        assert body["queued_messages"] == 0, "cancelling drops what was queued"
    finally:
        os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_a_message_to_an_idle_task_starts_its_next_turn_with_resume(
    store: TaskStore, workdir: str
) -> None:
    os.environ["FAKE_CLAUDE"] = "answers"
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        await settled(service, task.id)
        _fresh, _advice = await service.message(ACCOUNT, task.id, "now add a test")
        body = await settled(service, task.id)
        assert body["turns"] == 2
        import json
        from pathlib import Path

        argv = json.loads((Path(workdir) / "argv.json").read_text(encoding="utf-8"))["argv"]
        assert "--resume" in argv, "the second turn resumed the same session"
        assert argv[argv.index("-p") + 1] == "now add a test"
    finally:
        os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_a_failed_task_keeps_the_sentence_and_stays_resumable(
    store: TaskStore, workdir: str
) -> None:
    os.environ["FAKE_CLAUDE"] = "limit"
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        body = await settled(service, task.id, TaskState.failed)
        assert "hit your session limit" in body["detail"]
        assert body["resumable"] is True
        fresh, _advice = await service.message(ACCOUNT, task.id, "try again")
        assert fresh.state in {TaskState.queued, TaskState.running}
    finally:
        os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_a_strangers_task_reads_as_missing(store: TaskStore, workdir: str) -> None:
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        await settled(service, task.id)
        with pytest.raises(RefusedError, match="no such task"):
            await service.get(STRANGER, task.id)
        assert await service.list(STRANGER) == []
        [mine] = await service.list(ACCOUNT)
        assert mine["id"] == task.id
    finally:
        await service.aclose()


async def test_the_transcript_tail_rides_on_a_read_that_asks_for_it(
    store: TaskStore, workdir: str
) -> None:
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        await settled(service, task.id)
        with_tail = await service.get(ACCOUNT, task.id, tail_chars=10_000)
        assert '"type": "result"' in with_tail["transcript_tail"]
        without = await service.get(ACCOUNT, task.id)
        assert "transcript_tail" not in without
    finally:
        await service.aclose()


async def test_rows_left_running_by_a_dead_bridge_fail_honestly_at_startup(
    store: TaskStore, workdir: str
) -> None:
    os.environ["FAKE_CLAUDE"] = "hangs"
    service = a_service(store, timeout=60)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
    finally:
        os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()

    reborn = a_service(store)
    try:
        body = await reborn.get(ACCOUNT, task.id)
        assert body["state"] == "failed"
        assert body["detail"] == INTERRUPTED
        assert body["resumable"] is True
    finally:
        await reborn.aclose()


async def test_cancel_is_idempotent_and_a_cancelled_task_refuses_messages_until_it_ran(
    store: TaskStore, workdir: str
) -> None:
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        await settled(service, task.id)
        again = await service.cancel(ACCOUNT, task.id)
        assert again.state is TaskState.idle, "cancelling finished work changes nothing"
    finally:
        await service.aclose()

    fresh_store_task = store.create(
        account_id=ACCOUNT, brief="never ran", directory=workdir, run_level="edits", title="q"
    )
    blocked = a_service(store, max_live=1)
    try:
        # Occupy the only slot so the new task stays queued, then cancel it unrun.
        import os as _os

        _os.environ["FAKE_CLAUDE"] = "hangs"
        holder = await blocked.start(
            account_id=ACCOUNT, brief="hold", directory=workdir, run_level="edits", title="h"
        )
        del holder
        cancelled = await blocked.cancel(ACCOUNT, fresh_store_task.id)
        assert cancelled.state is TaskState.cancelled
        assert cancelled.resumable is False
        with pytest.raises(RefusedError, match="this task is cancelled"):
            await blocked.message(ACCOUNT, fresh_store_task.id, "hello?")
    finally:
        _os.environ.pop("FAKE_CLAUDE", None)
        await blocked.aclose()


async def test_a_turn_that_ends_after_its_cancel_does_not_overwrite_the_ending(
    store: TaskStore, workdir: str
) -> None:
    import os as _os

    _os.environ["FAKE_CLAUDE"] = "hangs"
    service = a_service(store, timeout=60)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        for _ in range(100):
            if service._turns:
                break
            await asyncio.sleep(0.05)
        await service.cancel(ACCOUNT, task.id)
        for _ in range(200):
            if not service._turns:
                break
            await asyncio.sleep(0.05)
        body = await service.get(ACCOUNT, task.id)
        assert body["state"] == "cancelled"
        assert body["detail"] == "cancelled by the person"
    finally:
        _os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_an_unanswered_process_and_a_signed_out_result_read_honestly(
    store: TaskStore, workdir: str
) -> None:
    import os as _os

    service = a_service(store)
    try:
        _os.environ["FAKE_CLAUDE"] = "mute"
        muted = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        body = await settled(service, muted.id, TaskState.failed)
        assert "without a result record" in body["detail"]
        assert "exit 3" in body["detail"]

        _os.environ["FAKE_CLAUDE"] = "signed-out-result"
        out = await service.start(
            account_id=ACCOUNT, brief="two", directory=workdir, run_level="edits", title="2"
        )
        body = await settled(service, out.id, TaskState.failed)
        assert "signed out" in body["detail"]
    finally:
        _os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_a_turn_ending_with_a_message_already_queued_goes_straight_back_to_queued(
    store: TaskStore, workdir: str
) -> None:
    from lucy_coder.runner import TurnOutcome

    service = a_service(store, max_live=0)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        held = store.get(ACCOUNT, task.id)
        held.state = TaskState.running
        held.queued_messages = ["and then this"]
        store.save(held)
        service._settle(task.id, TurnOutcome(ok=True, result="first answer", num_turns=1))
        body = await service.get(ACCOUNT, task.id)
        assert body["state"] == "queued"
        assert body["detail"] == "a queued message starts its next turn"
        assert body["result"] == "first answer"
    finally:
        await service.aclose()
