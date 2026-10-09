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
        assert fresh.queued_messages == [{"text": "also add a test", "mode": "", "allow_tools": []}]

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

    blocked = a_service(store, max_live=1)
    import os as _os

    _os.environ["FAKE_CLAUDE"] = "hangs"
    try:
        # The first takes the only slot and hangs there; the second waits, unrun.
        holder = await blocked.start(
            account_id=ACCOUNT, brief="hold", directory=workdir, run_level="edits", title="h"
        )
        waiting = await blocked.start(
            account_id=ACCOUNT, brief="never ran", directory=workdir, run_level="edits", title="q"
        )
        assert (await blocked.get(ACCOUNT, holder.id))["state"] == "running"
        assert (await blocked.get(ACCOUNT, waiting.id))["state"] == "queued"

        cancelled = await blocked.cancel(ACCOUNT, waiting.id)
        assert cancelled.state is TaskState.cancelled
        assert cancelled.resumable is False, "it never had a session to resume"
        assert cancelled.detail == "cancelled by the person before it started"
        with pytest.raises(RefusedError, match="this task is cancelled"):
            await blocked.message(ACCOUNT, waiting.id, "hello?")
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
        assert body["detail"] == "interrupted by the person; a message resumes the session"
        assert body["resumable"] is True, "Esc, not delete: the session is still there"
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


async def test_progress_never_writes_over_a_task_that_is_no_longer_running(
    store: TaskStore, workdir: str
) -> None:
    """A late stream event after a cancel, or for a row that vanished, changes nothing --
    deterministic here, where the live race only sometimes reaches it."""
    from lucy_coder.runner import Counters

    service = a_service(store, max_live=0)
    try:
        task = await service.start(
            account_id=ACCOUNT, brief="one", directory=workdir, run_level="edits", title="1"
        )
        late = Counters(tool_uses=9, last_tool="Bash", last_text="too late")
        service._progress(task.id, late)
        service._progress("tsk_gone", late)
        row = store.get(ACCOUNT, task.id)
        assert row is not None
        assert (row.tool_uses, row.last_tool) == (0, ""), "a queued row is not overwritten"
    finally:
        await service.aclose()


def _argv(workdir: str) -> list[str]:
    import json as _json
    from pathlib import Path as _Path

    return list(_json.loads((_Path(workdir) / "argv.json").read_text(encoding="utf-8"))["argv"])


async def test_plan_first_then_carry_it_out_in_the_same_session(
    store: TaskStore, workdir: str
) -> None:
    """The normal user's flow: plan mode explores and proposes; on their yes the same session
    resumes at `edits` and does it. The mode sticks for the turns after."""
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT,
            brief="plan a --version flag",
            directory=workdir,
            run_level="plan",
            title="v",
        )
        await settled(service, task.id)
        argv = _argv(workdir)
        assert argv[argv.index("--permission-mode") + 1] == "plan"

        await service.message(ACCOUNT, task.id, "looks good, do it", mode="edits")
        body = await settled(service, task.id)
        argv = _argv(workdir)
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert "--resume" in argv
        assert body["run_level"] == "edits", "the row says the level it runs at now"

        await service.message(ACCOUNT, task.id, "and update the readme")
        await settled(service, task.id)
        argv = _argv(workdir)
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits", "a mode sticks"
    finally:
        await service.aclose()


async def test_a_refused_tool_is_on_the_row_and_a_yes_resumes_with_it_allowed(
    store: TaskStore, workdir: str
) -> None:
    import os as _os

    _os.environ["FAKE_CLAUDE"] = "denied"
    service = a_service(store)
    try:
        task = await service.start(
            account_id=ACCOUNT,
            brief="write denied.txt",
            directory=workdir,
            run_level="ask",
            title="d",
            model="sonnet",
        )
        body = await settled(service, task.id)
        assert [denial["tool"] for denial in body["permission_denials"]] == ["Write"]
        assert body["model"] == "sonnet"

        _os.environ["FAKE_CLAUDE"] = "answers"
        await service.message(ACCOUNT, task.id, "approved - go ahead", allow_tools=("Write",))
        body = await settled(service, task.id)
        argv = _argv(workdir)
        assert argv[argv.index("--allowedTools") + 1] == "Write"
        assert argv[argv.index("--model") + 1] == "sonnet", "the task's model holds"
        assert body["permission_denials"] == [], "this turn was refused nothing"
    finally:
        _os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_what_a_follow_up_may_ask_for_is_checked(store: TaskStore, workdir: str) -> None:
    service = a_service(store)
    try:
        with pytest.raises(RefusedError, match="model must be"):
            await service.start(
                account_id=ACCOUNT,
                brief="x",
                directory=workdir,
                run_level="edits",
                title="",
                model="--dangerously",
            )
        task = await service.start(
            account_id=ACCOUNT, brief="x", directory=workdir, run_level="plan", title=""
        )
        await settled(service, task.id)
        with pytest.raises(RefusedError, match="run_level must be one of"):
            await service.message(ACCOUNT, task.id, "go", mode="yolo")
        too_many = tuple(f"T{index}" for index in range(11))
        for bad in (("Write --dangerously-skip",), ("Bash(a)(b)",), too_many):
            with pytest.raises(RefusedError, match="allow_tools takes"):
                await service.message(ACCOUNT, task.id, "go", allow_tools=bad)
        with pytest.raises(RefusedError, match="plan mode is read-only"):
            await service.message(ACCOUNT, task.id, "go", allow_tools=("Write",))
        with pytest.raises(RefusedError, match="plan mode is read-only"):
            await service.message(ACCOUNT, task.id, "go", mode="plan", allow_tools=("Write",))
    finally:
        await service.aclose()


async def test_esc_then_a_message_carries_on_in_the_same_session(
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
        stopped = await service.cancel(ACCOUNT, task.id)
        assert stopped.resumable is True
        for _ in range(200):
            if not service._turns:
                break
            await asyncio.sleep(0.05)

        _os.environ["FAKE_CLAUDE"] = "answers"
        revived, _advice = await service.message(ACCOUNT, task.id, "sorry, carry on")
        assert revived.state in {TaskState.queued, TaskState.running}
    finally:
        _os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()


async def test_a_relative_folder_is_refused_before_it_can_mean_the_bridges_own(
    store: TaskStore,
) -> None:
    """`.` in the person's list would have run Claude Code in whatever folder the bridge was
    started from -- its own checkout."""
    service = a_service(store)
    try:
        for relative in (".", "src", "../elsewhere"):
            with pytest.raises(RefusedError, match="must be an absolute path"):
                await service.start(
                    account_id=ACCOUNT,
                    brief="x",
                    directory=relative,
                    run_level="edits",
                    title="",
                )
    finally:
        await service.aclose()


async def test_a_resumed_turn_does_not_show_the_last_turn_s_answer_or_refusals(
    store: TaskStore, workdir: str
) -> None:
    """The bug, named: a follow-up's result and a read while it ran echoed the previous
    turn's answer and permission denials, so Lucy told the person the old answer twice and
    asked for a yes to a refusal that was already over."""
    os.environ["FAKE_CLAUDE"] = "denied"
    service = a_service(store, timeout=60)
    try:
        task = await service.start(
            account_id=ACCOUNT,
            brief="write denied.txt",
            directory=workdir,
            run_level="ask",
            title="d",
        )
        body = await settled(service, task.id)
        assert body["permission_denials"]
        assert body["result"]

        os.environ["FAKE_CLAUDE"] = "hangs"
        await service.message(ACCOUNT, task.id, "append a second line")
        running = await service.get(ACCOUNT, task.id)
        assert running["state"] == "running"
        assert running["result"] == "", "the last turn's answer is not this turn's"
        assert running["permission_denials"] == [], "nor are its refusals"
        assert running["turns"] == body["turns"], "the session's counters stay"

        os.environ["FAKE_CLAUDE"] = "answers"
        await service.cancel(ACCOUNT, task.id)
    finally:
        os.environ.pop("FAKE_CLAUDE", None)
        await service.aclose()
