"""The one place work that outlives a step is started, watched, fetched and stopped.

Everything long-running goes through here -- a helper agent, a download, a shell command --
because the alternative is three mechanisms with three subtly different answers to "was it
cancelled or did it time out?", and a model that has to learn all three.

The shape, in the order it happens:

**Starting returns a handle, immediately.** `start` schedules the work and comes back. The
step that called it completes; the work does not. The handle is stable and survives the end
of the turn.

**The turn carries on.** "The download is going, I will tell you when it lands" is a
complete reply, and a person prefers it to a four-minute silence.

**Completion arrives as a notice at the next tool boundary.** `drain` is called there and
nowhere else: never mid-tool, because a tool that is half-applied when its caller changes
its mind leaves a file half-written, and never mid-model-call, because the request has
already been sent.

**The result is fetched, not pushed.** A notice says a thing finished and roughly how big
the answer is. `result` is a separate, explicit act.

**A timeout fires and says so.** Nothing waits forever, and `timed_out` is a different fact
from `failed`.

**Cancelling is explicit and idempotent**, and is never a side effect of a client
disconnecting.

Nothing in here touches a model, a database or a socket. It holds records and asyncio tasks,
which is what makes it testable without any of those.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Coroutine
from typing import TYPE_CHECKING

from lucy_api.context.types import WorkSnapshot
from lucy_api.work.types import (
    MAX_OBJECTIVE,
    MAX_PROGRESS,
    MAX_ROLE,
    Brief,
    Handle,
    Kind,
    Notice,
    Record,
    Result,
    State,
    WorkError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    type Listener = Callable[[Record], Awaitable[None]]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 600.0
"""How long a piece of work runs before it is stopped and told so.

Ten minutes, because the things that live here are legitimately slow -- a helper reading
forty files, a download of an hour-long recording -- and failing them at the thirty seconds
an HTTP client would like only produces a retry that also takes ten minutes. Callers that
know better pass their own.
"""

MAX_CONCURRENT = 20
"""How many things may run at once for one session.

A cap the model is *told about* rather than one that raises: "you are at the limit, wait for
one of these or do it inline" is something a model can act on, and an exception is not.
"""

KEEP_FINISHED = 50
"""How many finished records are retained per session before the oldest are dropped.

Finished work is kept so that a result can still be fetched after the notice was read, and
so the live block can say what ended. Kept forever it is a leak, and the fiftieth-oldest
completed download is not something anybody is about to ask for.
"""


class AtCapacityError(Exception):
    """Raised when a session already has `MAX_CONCURRENT` things running.

    It is an exception here and a sentence by the time the model sees it. The boundary that
    turns one into the other is the operation, which knows how to phrase a cap as advice.
    """


class UnknownWorkError(Exception):
    """Raised when an id names nothing this session started.

    Never "not found" silently: a model that asked about a handle it invented needs to be
    told it invented it, or it will ask again.
    """


class StillRunningError(Exception):
    """Raised when a result is fetched before there is one.

    The right move is to wait for the notice, not to poll, and the message says so.
    """


def new_id() -> str:
    """A handle nobody can guess. Public so a caller that needs the id before the work
    starts -- a watch that reports progress under its own name -- can mint it first."""
    return "wrk_" + secrets.token_urlsafe(12)


def _discard(work: Awaitable[object]) -> None:
    """Close work that will never be awaited, when it is a coroutine.

    A coroutine garbage-collected without ever having run warns from wherever the collector
    happens to be, long after the code that created it returned, and points at a line that
    did nothing wrong. A task or a future is already owned by the loop and is left alone.
    """
    if isinstance(work, Coroutine):
        work.close()


def _clip(text: str, limit: int) -> str:
    """One line, bounded, with the cut made visible rather than silent."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


class Registry:
    """Every piece of in-flight work, per session.

    One instance per process. It is not thread-safe and does not need to be: everything that
    touches it runs on the event loop, which is the same reason the hub keeps one writer
    thread for SQLite rather than a lock around every statement.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        max_concurrent: int = MAX_CONCURRENT,
        keep_finished: int = KEEP_FINISHED,
        measure: Callable[[object], int] | None = None,
    ) -> None:
        self._now = now
        self._max_concurrent = max_concurrent
        self._keep_finished = keep_finished
        self._measure = measure or rough_tokens
        self._records: dict[str, Record] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._listeners: list[Listener] = []
        self._deliveries: set[asyncio.Task[None]] = set()

    def on_finished(self, listener: Listener) -> None:
        """Be told, once, about every ending -- after it has been recorded.

        This is how the rest of the hub learns that work ended without the registry knowing
        what the rest of the hub is: the stream gets an event, and a session that asked to be
        woken gets a turn. Listeners run as their own tasks, after the record is final, so a
        slow or failing listener cannot hold up or corrupt the ending it is being told about.
        """
        self._listeners.append(listener)

    # ---------------------------------------------------------------- starting

    def start(self, work: Awaitable[object], brief: Brief, *, work_id: str | None = None) -> Handle:
        """Schedule something that will outlive this step, and come back at once.

        The two arguments are the two halves of the idea: `work` is what runs, `brief` is
        everything anybody will later need to know about it. They are separate because the
        brief is written by whoever decided to start the work, and it has to survive long
        after the coroutine has been consumed.

        Raises `AtCapacityError` when the session is already at its limit. That is
        deliberately not a queue: a model told "you have twenty things running" makes a
        better decision than one whose twenty-first silently waits behind the other twenty.

        A refusal closes the coroutine it was handed. A caller writes `start(download(), ...)`
        and the coroutine exists before the refusal does; leaving it unawaited produces a
        warning from the garbage collector minutes later, pointing at a line that did nothing
        wrong. Cleaning it up here is the only place that can.
        """
        if len(self.running(brief.session_id)) >= self._max_concurrent:
            _discard(work)
            message = (
                f"{self._max_concurrent} things are already running for this session; "
                "wait for one to finish, cancel one, or do this inline"
            )
            raise AtCapacityError(message)

        identifier = work_id or new_id()
        if identifier in self._records:
            _discard(work)
            message = f"work {identifier} is already registered"
            raise ValueError(message)
        record = Record(
            id=identifier,
            kind=brief.kind,
            role=_clip(brief.role, MAX_ROLE),
            objective=_clip(brief.objective, MAX_OBJECTIVE),
            session_id=brief.session_id,
            started_at=self._now(),
            depth=brief.depth,
            timeout_seconds=brief.timeout_seconds,
            tags=dict(brief.tags),
            account_id=brief.account_id,
            wake=brief.wake and bool(brief.account_id),
        )
        self._records[record.id] = record
        self._tasks[record.id] = asyncio.create_task(
            self._run(record, work), name=f"work:{record.id}"
        )
        return Handle(
            id=record.id,
            kind=record.kind,
            role=record.role,
            objective=record.objective,
            started_at=record.started_at,
        )

    async def _run(self, record: Record, work: Awaitable[object]) -> None:
        """Await the work, and record what happened to it however it ended.

        Every ending is a state rather than an escape: a raised exception becomes `failed`
        carrying its **type name only**, because a third-party error routinely carries the
        response body that caused it, and that body is exactly the thing that must not reach
        a prompt or a log.
        """
        try:
            payload = await self._awaited(record, work)
        except TimeoutError:
            self._finish(record, State.timed_out, detail=_expired(record))
        except asyncio.CancelledError:
            self._finish(record, State.cancelled, detail="cancelled")
            raise
        except WorkError as exc:
            self._finish(record, State.failed, detail=str(exc))
        except Exception as exc:
            self._finish(record, State.failed, detail=type(exc).__name__)
        else:
            self._finish(record, State.succeeded, payload=payload)
        finally:
            self._tasks.pop(record.id, None)
            self._forget_old(record.session_id)

    @staticmethod
    async def _awaited(record: Record, work: Awaitable[object]) -> object:
        if record.timeout_seconds > 0:
            return await asyncio.wait_for(work, record.timeout_seconds)
        return await work

    def _finish(
        self, record: Record, state: State, *, payload: object = None, detail: str = ""
    ) -> None:
        """Record how a piece of work ended. Called exactly once per record.

        There is no guard against being called twice because there is no second caller:
        `_run` has one ending, and `cancel` asks the task to end rather than ending the
        record itself. A guard here would be a branch nothing can reach, which is a worse
        thing to have than the invariant written down.
        """
        record.state = state
        record.finished_at = self._now()
        record.payload = payload
        record.detail = _clip(detail, MAX_PROGRESS)
        record.tokens = self._measure(payload) if payload is not None else 0
        for listener in self._listeners:
            task = asyncio.create_task(_told(listener, record), name=f"work-told:{record.id}")
            self._deliveries.add(task)
            task.add_done_callback(self._deliveries.discard)

    # ---------------------------------------------------------------- checking in

    def state_of(self, work_id: str) -> State:
        """Where one piece of work has got to, for something that only needs the state.

        A watch on another piece of work asks this every few seconds. It is the one read
        that does not mark anything delivered, because it is not a delivery.
        """
        return self._record(work_id).state

    def progress(self, work_id: str, note: str) -> None:
        """Record what a running piece of work last said about itself.

        One line, overwritten rather than accumulated. A history of progress notes is a
        transcript, and a transcript is the thing this whole design exists to keep out of
        the context.
        """
        self._record(work_id).progress = _clip(note, MAX_PROGRESS)

    def drain(self, session_id: str) -> tuple[Notice, ...]:
        """Every completion this session has not yet been told about, told once.

        Called at a tool-call boundary and nowhere else. Delivery is marked before the
        caller does anything with the notices, so a failure downstream repeats a turn rather
        than announcing the same completion twice -- of the two, a missed notice is the one
        the live block fixes on its own next turn.
        """
        notices: list[Notice] = []
        for record in self._for(session_id):
            if not record.state.finished or record.noticed:
                continue
            record.noticed = True
            notices.append(record.notice(self._now()))
        self._forget_old(session_id)
        return tuple(notices)

    def snapshot(self, session_id: str, *, announce: bool = False) -> tuple[WorkSnapshot, ...]:
        """What the live-state block shows: everything in flight, in one group.

        `announce` is off by default so that rendering a preview of a prompt does not eat
        news a real turn has not seen yet. A turn that is genuinely being sent passes it.
        """
        now = self._now()
        snapshots: list[WorkSnapshot] = []
        for record in self._for(session_id):
            fresh = record.state.finished and not record.shown
            if record.state.finished and not fresh:
                continue
            if announce and fresh:
                record.shown = True
            snapshots.append(
                WorkSnapshot(
                    id=record.id,
                    role=record.role,
                    objective=record.objective,
                    status=record.state.value,
                    kind=record.kind.value,
                    depth=record.depth,
                    elapsed_seconds=record.elapsed(now),
                    progress=record.progress or record.detail,
                    finished_since_last_turn=fresh,
                )
            )
        return tuple(snapshots)

    def running(self, session_id: str) -> tuple[Record, ...]:
        """What is still going for one session, in the order it was started."""
        return tuple(record for record in self._for(session_id) if not record.state.finished)

    # ---------------------------------------------------------------- fetching

    def result(self, work_id: str) -> Result:
        """The answer, on purpose.

        Raises `StillRunningError` rather than blocking. A model that wants to wait has `wait`,
        which has a deadline; a model that polls in a loop has been given the wrong shape,
        and this signature is what stops that from being possible.
        """
        record = self._record(work_id)
        if not record.state.finished:
            message = f"{record.role} is still running; wait for its notice rather than polling"
            raise StillRunningError(message)
        record.fetched = True
        return Result(
            id=record.id,
            state=record.state,
            payload=record.payload,
            tokens=record.tokens,
            detail=record.detail,
        )

    async def wait(self, work_id: str, timeout_seconds: float) -> Result:
        """Wait for one piece of work, with a deadline that is always shorter than forever.

        A timeout here does not stop the work -- it stops the waiting. The distinction
        matters: somebody who asked "is it done yet?" is told "not yet", and the download
        carries on downloading.
        """
        record = self._record(work_id)
        task = self._tasks.get(work_id)
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
        if not record.state.finished:
            message = (
                f"{record.role} is still running after {timeout_seconds:.0f}s; "
                "it has not been stopped"
            )
            raise StillRunningError(message)
        return self.result(work_id)

    # ---------------------------------------------------------------- stopping

    def cancel(self, work_id: str) -> Record:
        """Stop something, explicitly. Calling it twice is calling it once.

        Idempotent because the alternative is a race nobody wins: somebody pressing stop
        twice, or a client retrying the request, must not be able to turn one cancellation
        into an error.
        """
        record = self._record(work_id)
        record.cancel_requested = True
        task = self._tasks.get(work_id)
        if task is not None:
            task.cancel()
        return record

    async def shutdown(self) -> None:
        """Stop everything still running, and wait for each to notice.

        A process going down with tasks still attached produces a shelf of "Task was
        destroyed but it is pending" warnings and, worse, work whose final state is never
        recorded. Everything here ends as a state.
        """
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        # Every cancellation above was an ending, and every ending has listeners. Let them
        # finish saying so; a process that exits mid-delivery loses the event.
        for delivery in list(self._deliveries):
            with contextlib.suppress(BaseException):
                await delivery

    # ---------------------------------------------------------------- internals

    def _record(self, work_id: str) -> Record:
        record = self._records.get(work_id)
        if record is None:
            message = f"{work_id!r} is not something this session started"
            raise UnknownWorkError(message)
        return record

    def _for(self, session_id: str) -> tuple[Record, ...]:
        return tuple(record for record in self._records.values() if record.session_id == session_id)

    def _forget_old(self, session_id: str) -> None:
        """Drop the oldest finished records once a session has more than it needs.

        Running work is never dropped, and neither is a finished record whose notice has not
        been delivered -- forgetting a completion before anybody was told about it is how a
        model ends up waiting for something that ended an hour ago.
        """
        finished = sorted(
            (
                record
                for record in self._for(session_id)
                if record.state.finished and record.noticed
            ),
            key=lambda record: record.finished_at or record.started_at,
        )
        for record in finished[: max(0, len(finished) - self._keep_finished)]:
            self._records.pop(record.id, None)


def _expired(record: Record) -> str:
    """The sentence for a timeout, which means two different things for two kinds of work.

    A helper or a command that hit its ceiling may well still be running somewhere, and the
    person is owed that doubt. A watch that hit its ceiling simply never saw what it was
    waiting for, and what the person is owed is the offer to look again.
    """
    if record.kind is Kind.watch:
        return (
            f"expired after {record.timeout_seconds:.0f}s without firing; "
            "start it again if you still need it"
        )
    return f"stopped waiting after {record.timeout_seconds:.0f}s; it may still be running"


async def _told(listener: Listener, record: Record) -> None:
    """One listener, one ending. A listener that raises is logged and does not stop the rest.

    The log line carries the exception's type and the work's id, never the payload: a
    listener fails on the way to a database or a stream, and the payload is the one thing
    in reach that might be somebody's file.
    """
    try:
        await listener(record)
    except Exception as exc:
        logger.warning(
            "work listener failed",
            extra={"work_id": record.id, "error": type(exc).__name__},
        )


def rough_tokens(payload: object) -> int:
    """About how many tokens a result would cost, to the nearest order of usefulness.

    Four characters to a token: free, wrong by a little, and never wrong in the direction
    that matters. The number exists so a model can decide whether something is worth
    reading, not so that anybody can bill for it.
    """
    text = payload if isinstance(payload, str) else repr(payload)
    return (len(text) + 3) // 4


def notices_block(notices: Sequence[Notice]) -> str:
    """The notices, as the paragraph handed back at a tool boundary.

    Empty when there is nothing to say. A heading over no lines is a heading that teaches
    the model to skim past the heading.
    """
    if not notices:
        return ""
    lines = "\n".join("  - " + notice.line() for notice in notices)
    return (
        "Work that finished while you were busy. Nothing here is the result itself; "
        "fetch one by its id to read it.\n" + lines
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "KEEP_FINISHED",
    "MAX_CONCURRENT",
    "AtCapacityError",
    "Registry",
    "StillRunningError",
    "UnknownWorkError",
    "new_id",
    "notices_block",
    "rough_tokens",
]
