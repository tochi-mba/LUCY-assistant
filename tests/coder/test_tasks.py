"""Rows and transcripts: durable, account-scoped, and honest about interruption."""

from __future__ import annotations

from typing import TYPE_CHECKING

from conftest import ACCOUNT, STRANGER

from lucy_coder.tasks import INTERRUPTED, RESULT_CHARS, TaskState

if TYPE_CHECKING:
    from lucy_coder.tasks import TaskStore


def a_task(store: TaskStore, brief: str = "do the thing") -> object:
    return store.create(
        account_id=ACCOUNT, brief=brief, directory="C:/repo", run_level="edits", title="t"
    )


def test_a_row_survives_a_round_trip_with_every_field(store: TaskStore) -> None:
    task = a_task(store)
    task.state = TaskState.idle
    task.detail = "the turn is over"
    task.result = "it compiled"
    task.turns = 2
    task.cost_usd = 0.42
    task.tool_uses = 7
    task.last_tool = "Edit"
    task.last_text = "editing now"
    task.queued_messages = ["one more thing"]
    task.resumable = True
    store.save(task)

    read = store.get(ACCOUNT, task.id)
    assert read is not None
    assert read.__dict__ == task.__dict__


def test_a_strangers_read_is_none_and_get_any_is_for_the_service_alone(
    store: TaskStore,
) -> None:
    task = a_task(store)
    assert store.get(STRANGER, task.id) is None
    assert store.get(ACCOUNT, "tsk_missing") is None
    found = store.get_any(task.id)
    assert found is not None
    assert found.id == task.id
    assert store.get_any("tsk_missing") is None


def test_the_queue_is_oldest_first_and_live_counts_running_alone(store: TaskStore) -> None:
    first = a_task(store, "first")
    second = a_task(store, "second")
    assert store.next_queued().id == first.id
    first.state = TaskState.running
    store.save(first)
    assert store.next_queued().id == second.id
    assert store.live_count() == 1
    second.state = TaskState.idle
    store.save(second)
    assert store.next_queued() is None


def test_interruption_fails_only_what_was_running_and_keeps_it_resumable(
    store: TaskStore,
) -> None:
    running = a_task(store, "running")
    running.state = TaskState.running
    store.save(running)
    done = a_task(store, "done")
    done.state = TaskState.idle
    store.save(done)

    assert store.mark_interrupted() == 1

    failed = store.get(ACCOUNT, running.id)
    assert failed.state is TaskState.failed
    assert failed.detail == INTERRUPTED
    assert failed.resumable is True
    assert store.get(ACCOUNT, done.id).state is TaskState.idle


def test_the_result_a_row_keeps_is_capped(store: TaskStore) -> None:
    task = a_task(store)
    task.result = "x" * (RESULT_CHARS + 5_000)
    store.save(task)
    assert len(store.get(ACCOUNT, task.id).result) == RESULT_CHARS


def test_transcripts_append_and_tail_from_the_end(store: TaskStore) -> None:
    task = a_task(store)
    assert store.transcript_tail(task.id, 100) == "", "no transcript reads as empty"
    store.append_transcript(task.id, '{"type":"system"}')
    store.append_transcript(task.id, '{"type":"result"}\n')
    whole = store.transcript_tail(task.id, 10_000)
    assert whole == '{"type":"system"}\n{"type":"result"}\n'
    assert store.transcript_tail(task.id, 10) == whole[-10:]


def test_listing_is_the_accounts_own_oldest_first(store: TaskStore) -> None:
    mine = a_task(store, "mine")
    store.create(
        account_id=STRANGER, brief="theirs", directory="C:/repo", run_level="edits", title="t"
    )
    listed = store.for_account(ACCOUNT)
    assert [task.id for task in listed] == [mine.id]
