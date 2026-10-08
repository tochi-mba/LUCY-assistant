"""The bridge's one brain: slots, the queue, follow-ups, and honest endings.

Two tasks run at once (the owner's number); the rest queue in the order they arrived and
start on their own as slots free. A follow-up to a running task queues *inside* the task --
the CLI would happily run two turns on one session concurrently, and the transcript race
is nobody's friend -- and runs the moment its turn ends. Everything a turn learns lands on
the durable row, so "check on that refactor from yesterday" works from any session.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_coder.runner import Counters, TurnOutcome
from lucy_coder.tasks import LIVE, SNIPPET_CHARS, Task, TaskState

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_coder.runner import ClaudeRunner
    from lucy_coder.tasks import TaskStore

logger = logging.getLogger(__name__)

RUN_LEVELS_ALLOWED = ("plan", "edits", "full")

NO_SUCH_TASK = "no such task"
NOT_A_DIRECTORY = "directory does not exist on this machine: {path}"
BAD_RUN_LEVEL = "run_level must be one of plan, edits, full"
EMPTY_BRIEF = "say what the task is, in at least a sentence"
ALREADY_OVER = "this task is {state}; start a new one, or message it to resume the session"
QUEUED_BEHIND = "queued behind {count} running task(s); it starts on its own"
MESSAGE_QUEUED = "the task is mid-turn; your message runs when this turn ends"


class RefusedError(Exception):
    """A request the bridge will not act on, with the sentence that says why."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class CoderService:
    """Owns the store and the runner; every route goes through here."""

    def __init__(self, store: TaskStore, runner: ClaudeRunner, *, max_live: int) -> None:
        self._store = store
        self._runner = runner
        self._max_live = max_live
        self._turns: dict[str, asyncio.Task[None]] = {}
        interrupted = store.mark_interrupted()
        if interrupted:
            logger.info("tasks_interrupted_at_startup", extra={"count": interrupted})

    # ------------------------------------------------------------------ starting

    async def start(
        self, *, account_id: str, brief: str, directory: str, run_level: str, title: str
    ) -> Task:
        if not brief.strip():
            raise RefusedError(422, EMPTY_BRIEF)
        if run_level not in RUN_LEVELS_ALLOWED:
            raise RefusedError(422, BAD_RUN_LEVEL)
        if not await asyncio.to_thread(Path(directory).is_dir):
            # The allowlist is the hub's to enforce from the person's settings; existence
            # is the bridge's, because only the host knows its own disk.
            raise RefusedError(422, NOT_A_DIRECTORY.format(path=directory))
        task = await asyncio.to_thread(
            self._store.create,
            account_id=account_id,
            brief=brief.strip(),
            directory=directory,
            run_level=run_level,
            title=title.strip() or brief.strip()[:60],
        )
        self._pump()
        return (await self._fresh(account_id, task.id)) or task

    async def message(self, account_id: str, task_id: str, text: str) -> tuple[Task, str]:
        """Queue one follow-up turn. Returns the task and a sentence about when it runs."""
        if not text.strip():
            raise RefusedError(422, "say what to tell it")
        task = await self._found(account_id, task_id)
        if task.state in {TaskState.failed, TaskState.cancelled} and not task.resumable:
            raise RefusedError(409, ALREADY_OVER.format(state=task.state.value))
        task.queued_messages.append(text.strip())
        if task.state in {TaskState.idle, TaskState.failed, TaskState.cancelled}:
            # A resumable ended task comes back to life; its next turn is the message's.
            task.state = TaskState.queued
        await asyncio.to_thread(self._store.save, task)
        self._pump()
        fresh = await self._found(account_id, task_id)
        said = MESSAGE_QUEUED if fresh.state is TaskState.running else QUEUED_BEHIND
        return fresh, said if said is MESSAGE_QUEUED else said.format(count=self._live())

    async def cancel(self, account_id: str, task_id: str) -> Task:
        """Stop a task. Idempotent: cancelling twice is cancelling once."""
        task = await self._found(account_id, task_id)
        if task.state not in LIVE:
            return task
        task.state = TaskState.cancelled
        task.detail = "cancelled by the person"
        task.queued_messages = []
        await asyncio.to_thread(self._store.save, task)
        await self._runner.cancel(task_id)
        self._pump()
        return await self._found(account_id, task_id)

    # ------------------------------------------------------------------ reading

    async def get(self, account_id: str, task_id: str, *, tail_chars: int = 0) -> dict[str, Any]:
        task = await self._found(account_id, task_id)
        body = task.public()
        if tail_chars > 0:
            body["transcript_tail"] = await asyncio.to_thread(
                self._store.transcript_tail, task_id, tail_chars
            )
        return body

    async def list(self, account_id: str) -> list[dict[str, Any]]:
        rows = await asyncio.to_thread(self._store.for_account, account_id)
        return [task.public() for task in rows]

    async def aclose(self) -> None:
        """Stop watching. The claude processes themselves are cancelled, not abandoned."""
        for task_id in list(self._turns):
            await self._runner.cancel(task_id)
        for turn in self._turns.values():
            turn.cancel()
        await asyncio.gather(*self._turns.values(), return_exceptions=True)
        self._turns.clear()

    # ------------------------------------------------------------------ the pump

    def _pump(self) -> None:
        """Start queued turns while there are slots. Synchronous and re-entrant-safe:
        everything it reads is re-read from the store, and starting is idempotent because
        a started task is `running` before the next pump looks."""
        while self._live() < self._max_live:
            task = self._store.next_queued()
            if task is None:
                return
            prompt = task.queued_messages.pop(0) if task.turns else task.brief
            resume = task.turns > 0
            task.state = TaskState.running
            self._store.save(task)
            turn = asyncio.get_running_loop().create_task(
                self._run_turn(task, prompt, resume=resume),
                name=f"coder-turn-{task.id}",
            )
            self._turns[task.id] = turn
            turn.add_done_callback(_forgetting(self._turns, task.id))

    def _live(self) -> int:
        return self._store.live_count()

    async def _run_turn(self, task: Task, prompt: str, *, resume: bool) -> None:
        counters = Counters(on_change=lambda seen: self._progress(task.id, seen))
        outcome = await self._runner.run_turn(
            task_id=task.id,
            prompt=prompt,
            cwd=task.directory,
            session_id=task.session_id,
            resume=resume,
            run_level=task.run_level,
            counters=counters,
            transcribe=lambda line: self._store.append_transcript(task.id, line),
        )
        await asyncio.to_thread(self._settle, task.id, outcome)
        self._pump()

    def _settle(self, task_id: str, outcome: TurnOutcome) -> None:
        task = self._store.get_any(task_id)
        if task is None or task.state is TaskState.cancelled:
            # Cancelled mid-turn: the row already says so, and says why.
            return
        task.turns += outcome.num_turns or 1
        task.cost_usd += outcome.cost_usd
        task.resumable = True
        if not outcome.ok:
            task.state = TaskState.failed
            task.detail = outcome.detail
        elif task.queued_messages:
            task.state = TaskState.queued
            task.detail = "a queued message starts its next turn"
            task.result = outcome.result
        else:
            task.state = TaskState.idle
            task.detail = "the turn is over; a message resumes the session"
            task.result = outcome.result
        self._store.save(task)

    def _progress(self, task_id: str, counters: Counters) -> None:
        task = self._store.get_any(task_id)
        if task is None or task.state is not TaskState.running:
            return
        task.tool_uses = counters.tool_uses
        task.last_tool = counters.last_tool
        task.last_text = counters.last_text[:SNIPPET_CHARS]
        self._store.save(task)

    async def _found(self, account_id: str, task_id: str) -> Task:
        task = await self._fresh(account_id, task_id)
        if task is None:
            raise RefusedError(404, NO_SUCH_TASK)
        return task

    async def _fresh(self, account_id: str, task_id: str) -> Task | None:
        return await asyncio.to_thread(self._store.get, account_id, task_id)


def _forgetting(
    turns: dict[str, asyncio.Task[None]], task_id: str
) -> Callable[[asyncio.Task[None]], None]:
    """A done-callback that drops the turn's handle, typed where a lambda cannot be."""

    def forget(_done: asyncio.Task[None]) -> None:
        turns.pop(task_id, None)

    return forget
