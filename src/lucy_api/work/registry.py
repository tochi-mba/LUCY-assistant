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

**Work past a cap can wait its turn.** `queue` starts work at once when one of its kind's
slots is free and otherwise holds it, in order, until one is -- bounded, so a queue is never
a promise to run something after the person has stopped caring. Its clock starts when it
starts. `start` is the other answer, for work whose caller would rather be told no.

**A group ends once.** Work started under one group name is told, as a `Team`, when the last
of it ends, so five reviewers started together are one piece of news rather than five.

Nothing in here touches a model, a database or a socket. It holds records and asyncio tasks,
which is what makes it testable without any of those.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.context.types import WorkSnapshot
from lucy_api.work.types import (
    MAX_GROUP,
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
    Team,
    WorkError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime

    type Listener = Callable[[Record], Awaitable[None]]
    type TeamListener = Callable[[Team], Awaitable[None]]

logger = logging.getLogger(__name__)

RESUMED_AFTER_RESTART = frozenset({Kind.helper, Kind.subscription})
"""Kinds the next process picks up again, so a restart is not their ending to announce."""

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


CANCELLED_QUEUED = "cancelled before it started"
"""The ending of queued work somebody cancelled: nothing of it ever ran."""

RESTARTED = "the hub restarted while it was running"
"""Why work stopped when the process running it went down. Nobody stopped it."""


class AtCapacityError(Exception):
    """Raised when a session already has `MAX_CONCURRENT` things running, or a full queue.

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


@dataclass(frozen=True, slots=True)
class _Waiting:
    """Queued work: how to start it, how many of its kind may run, and who to tell if not.

    `work` makes the awaitable only when the work starts, so nothing of it -- not even a
    coroutine object -- exists while it waits, and its own clock cannot start early.
    """

    work: Callable[[], Awaitable[object]]
    slots: int
    dropped: Callable[[], Awaitable[None]] | None = None


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
        self._waiting: dict[str, _Waiting] = {}
        self._begun: dict[str, asyncio.Event] = {}
        self._listeners: list[Listener] = []
        self._team_listeners: list[TeamListener] = []
        self._teams: dict[str, list[Team]] = {}
        self._deliveries: set[asyncio.Task[None]] = set()
        self._closing = False

    @property
    def closing(self) -> bool:
        """Whether the process is going down. Work cancelled now was stopped by nobody."""
        return self._closing

    def on_finished(self, listener: Listener) -> None:
        """Be told, once, about every ending -- after it has been recorded.

        This is how the rest of the hub learns that work ended without the registry knowing
        what the rest of the hub is: the stream gets an event, and a session that asked to be
        woken gets a turn. Listeners run as their own tasks, after the record is final, so a
        slow or failing listener cannot hold up or corrupt the ending it is being told about.
        """
        self._listeners.append(listener)

    def on_team_finished(self, listener: TeamListener) -> None:
        """Be told, once, when the last member of a group ends -- after every member has.

        Told after each member's own ending, so a listener that wakes the session for the
        group can rely on every member's record being final.
        """
        self._team_listeners.append(listener)

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
        if len(self._active(brief.session_id)) >= self._max_concurrent:
            _discard(work)
            message = (
                f"{self._max_concurrent} things are already running for this session; "
                "wait for one to finish, cancel one, or do this inline"
            )
            raise AtCapacityError(message)

        identifier = work_id or new_id()
        if identifier in self._records:
            _discard(work)
            raise ValueError(_taken(identifier))
        record = self._new_record(identifier, brief, State.running)
        self._records[record.id] = record
        task = asyncio.create_task(self._run(record, work), name=f"work:{record.id}")
        task.add_done_callback(lambda done: self._never_ran(record, work, done))
        self._tasks[record.id] = task
        return _handle(record)

    def queue(  # noqa: PLR0913 - the work, its brief, and the three numbers of its lane
        self,
        work: Callable[[], Awaitable[object]],
        brief: Brief,
        *,
        slots: int,
        waiting: int,
        work_id: str | None = None,
        dropped: Callable[[], Awaitable[None]] | None = None,
    ) -> Handle:
        """Start work now if one of its kind's `slots` is free, or hold it until one is.

        `slots` is how many of this kind may run at once for the session, and is the
        person's number, not the registry's: five helpers, say. Past it the work is
        `queued`, in the order it arrived, and starts on its own when one of its kind ends --
        never more than `slots` at once, and never past the registry's own cap either. Its
        timeout counts from when it starts, because `work` is only called then.

        `waiting` bounds the queue. Past it this raises `AtCapacityError`, which the caller
        phrases as advice: a queue that grows without end is a promise to do the work after
        the person has stopped caring about it.

        `dropped` is told when queued work is cancelled before it ever started, since
        nothing of the work itself will run to say so.
        """
        identifier = work_id or new_id()
        if identifier in self._records:
            raise ValueError(_taken(identifier))
        if self._has_room(brief.session_id, brief.kind, slots):
            return self.start(work(), brief, work_id=identifier)
        held = [
            record
            for record in self._for(brief.session_id)
            if record.kind is brief.kind and record.state is State.queued
        ]
        if len(held) >= waiting:
            message = (
                f"{slots} {brief.kind.value}s may run at once and {len(held)} more are already "
                "queued, as many as may wait; wait for one to finish, cancel one, or do this "
                "inline"
            )
            raise AtCapacityError(message)
        record = self._new_record(identifier, brief, State.queued)
        self._records[record.id] = record
        self._waiting[record.id] = _Waiting(work=work, slots=slots, dropped=dropped)
        return _handle(record)

    def _new_record(self, identifier: str, brief: Brief, state: State) -> Record:
        return Record(
            id=identifier,
            kind=brief.kind,
            role=_clip(brief.role, MAX_ROLE),
            objective=_clip(brief.objective, MAX_OBJECTIVE),
            session_id=brief.session_id,
            started_at=self._now(),
            depth=brief.depth,
            timeout_seconds=brief.timeout_seconds,
            state=state,
            tags=dict(brief.tags),
            account_id=brief.account_id,
            wake=brief.wake and bool(brief.account_id),
            group=_clip(brief.group, MAX_GROUP),
        )

    def _active(self, session_id: str) -> tuple[Record, ...]:
        """What is actually running for one session: started, and not yet ended."""
        return tuple(record for record in self._for(session_id) if record.state is State.running)

    def _has_room(self, session_id: str, kind: Kind, slots: int) -> bool:
        active = self._active(session_id)
        of_kind = sum(1 for record in active if record.kind is kind)
        return len(active) < self._max_concurrent and of_kind < slots

    def _promote(self, session_id: str) -> None:
        """Start queued work, oldest first, for as long as its kind has a slot free.

        Called whenever something of the session ends. Not while the process is going
        down: work that never started is left queued, and the next process says so.
        """
        if self._closing:
            return
        for record in self._for(session_id):
            waiting = self._waiting.get(record.id)
            if waiting is None or not self._has_room(session_id, record.kind, waiting.slots):
                continue
            del self._waiting[record.id]
            self._left_queue(record.id)
            record.state = State.running
            record.started_at = self._now()
            self._tasks[record.id] = asyncio.create_task(
                self._run(record, waiting.work()), name=f"work:{record.id}"
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
            if self._closing:
                self._stopped_by_restart(record)
            else:
                self._finish(record, State.cancelled, detail="cancelled")
            raise
        except WorkError as exc:
            self._finish(record, State.failed, detail=str(exc), payload=exc.payload)
        except Exception as exc:
            self._finish(record, State.failed, detail=type(exc).__name__)
        else:
            self._finish(record, State.succeeded, payload=payload, detail=_said(record, payload))
        finally:
            self._tasks.pop(record.id, None)
            self._forget_old(record.session_id)
            self._promote(record.session_id)

    def _never_ran(self, record: Record, work: Awaitable[object], task: asyncio.Task[None]) -> None:
        """End work whose task was cancelled before it took its first step.

        A task cancelled before the loop first runs it never enters `_run`, so nothing records
        its ending and the record says `running` for ever -- with its work coroutine never
        awaited. Starting something and cancelling it straight away is ordinary (a sibling
        that refuses the subscription it was just asked for), so it ends here the way `_run`
        would have ended it. A task that did run has ended its record already.
        """
        if record.state is not State.running or not task.cancelled():
            return
        _discard(work)
        self._tasks.pop(record.id, None)
        if self._closing:
            self._stopped_by_restart(record)
        else:
            self._finish(record, State.cancelled, detail="cancelled")
        self._forget_old(record.session_id)
        self._promote(record.session_id)

    @staticmethod
    async def _awaited(record: Record, work: Awaitable[object]) -> object:
        if record.timeout_seconds > 0:
            return await asyncio.wait_for(work, record.timeout_seconds)
        return await work

    def _stopped_by_restart(self, record: Record) -> None:
        """Work this process stops on its way down: an ending nobody chose, not a cancellation.

        A subscription is not told about either: its row is durable and the next process
        takes it up again (`work.subscriptions`), so announcing it as stopped would be false.

        A helper is not told about here at all. Its row stays running, and the next process
        announces it as continuable (`agents.restart`). Told here, it was announced to its
        conversation as cancelled -- the person's own choice, never offered for continuing --
        and the model, asked what was interrupted, said nothing had been. Other work has no
        next process to speak for it, so it is told, as stopped by the restart.
        """
        if record.kind in RESUMED_AFTER_RESTART:
            self._finish(record, State.failed, detail=RESTARTED, tell=False)
            return
        self._finish(record, State.failed, detail=RESTARTED)

    def _finish(
        self,
        record: Record,
        state: State,
        *,
        payload: object = None,
        detail: str = "",
        tell: bool = True,
    ) -> None:
        """Record how a piece of work ended. Called exactly once per record.

        There is no guard against being called twice because there is no second caller:
        `_run` has one ending for work that started, and `cancel` ends only work that never
        did -- for started work it asks the task to end rather than ending the record itself.
        A guard here would be a branch nothing can reach, which is a worse thing to have
        than the invariant written down.
        """
        record.state = state
        record.finished_at = self._now()
        record.payload = payload
        record.detail = _clip(detail, MAX_PROGRESS)
        record.tokens = self._measure(payload) if payload is not None else 0
        if not tell:
            return
        for listener in self._listeners:
            self._deliver(_told(listener, record), f"work-told:{record.id}")
        if record.group:
            self._end_team(record.session_id, record.group)

    def _end_team(self, session_id: str, group: str) -> None:
        """Tell the group's ending once its last member has ended, and not before."""
        members = tuple(
            record
            for record in self._for(session_id)
            if record.group == group and not record.teamed
        )
        if any(not member.state.finished for member in members):
            return
        for member in members:
            member.teamed = True
        team = Team(session_id=session_id, group=group, members=members)
        told = self._teams.setdefault(session_id, [])
        told.append(team)
        del told[: max(0, len(told) - self._keep_finished)]
        for listener in self._team_listeners:
            self._deliver(_told_team(listener, team), f"work-team:{group}")

    def _deliver(self, told: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(told)
        task.set_name(name)
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    def record_lost(  # noqa: PLR0913 - the brief, and the four facts of how it ended
        self,
        brief: Brief,
        *,
        work_id: str,
        started_at: datetime,
        ended_at: datetime,
        detail: str,
        payload: object = None,
    ) -> None:
        """Remember work a previous process was running when it stopped, as a failed ending.

        That process took the work's task with it, and every notice it would have sent. The
        model that started the work was told a notice would come, and without this none
        ever does: the work simply vanishes. Recorded unshown and unnoticed, so the next turn's
        live block and `work.check` report it once, like any other ending.

        No listener is told. Listeners wake idle sessions, and a restart that woke every
        conversation it had interrupted would start them all at once. Calling this twice
        for one id is calling it once.
        """
        if work_id in self._records:
            return
        self._records[work_id] = Record(
            id=work_id,
            kind=brief.kind,
            role=_clip(brief.role, MAX_ROLE),
            objective=_clip(brief.objective, MAX_OBJECTIVE),
            session_id=brief.session_id,
            started_at=started_at,
            depth=brief.depth,
            state=State.failed,
            finished_at=max(ended_at, started_at),
            payload=payload,
            tokens=self._measure(payload) if payload is not None else 0,
            detail=_clip(detail, MAX_PROGRESS),
            account_id=brief.account_id,
            group=_clip(brief.group, MAX_GROUP),
            teamed=True,
        )

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

    def drain_teams(self, session_id: str) -> tuple[Team, ...]:
        """Every group of this session that ended since it was last asked, told once."""
        return tuple(self._teams.pop(session_id, ()))

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
                    # How it ended, once it has: a finished helper's last progress note
                    # ("reading page 3") stood where why it stopped belonged.
                    progress=(
                        (record.detail or record.progress)
                        if record.state.finished
                        else (record.progress or record.detail or _due_line(record, now))
                    ),
                    finished_since_last_turn=fresh,
                    group=record.group,
                )
            )
        return tuple(snapshots)

    def now(self) -> datetime:
        """The registry's clock, for a caller saying how far away a due time is."""
        return self._now()

    def running(self, session_id: str) -> tuple[Record, ...]:
        """What is still going for one session, in the order it was started.

        Queued work is here too: it is in flight, only not started. A caller that needs to
        tell them apart reads each record's state.
        """
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
            message = (
                f"{record.role} is still {record.state.value}; wait for its notice rather "
                "than polling"
            )
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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        if work_id in self._waiting:
            # Queued: wait for it to start, within the same deadline, and then for it to end.
            begun = self._begun.setdefault(work_id, asyncio.Event())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(begun.wait(), timeout_seconds)
        task = self._tasks.get(work_id)
        if task is not None:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), max(0.0, deadline - loop.time()))
        if not record.state.finished:
            message = (
                f"{record.role} is still {record.state.value} after {timeout_seconds:.0f}s; "
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
        waiting = self._waiting.pop(work_id, None)
        if waiting is not None:
            self._left_queue(work_id)
            # Nothing of it ever ran, so there is no task to stop and nothing of the work to
            # record its own ending: it ends here, and whoever queued it is told.
            self._finish(record, State.cancelled, detail=CANCELLED_QUEUED)
            if waiting.dropped is not None:
                self._deliver(_quietly(waiting.dropped(), record), f"work-dropped:{record.id}")
            return record
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
        self._closing = True
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

    def _left_queue(self, work_id: str) -> None:
        """Wake anything waiting for queued work to start: it has, or it never will."""
        begun = self._begun.pop(work_id, None)
        if begun is not None:
            begun.set()

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


def _handle(record: Record) -> Handle:
    return Handle(
        id=record.id,
        kind=record.kind,
        role=record.role,
        objective=record.objective,
        started_at=record.started_at,
    )


def _taken(identifier: str) -> str:
    return f"work {identifier} is already registered"


def _said(record: Record, payload: object) -> str:
    """The sentence a succeeded subscription's notice carries: its signal's summary.

    A helper's result is read on purpose, so its notice says only that there is one. A
    subscription's result *is* a sentence -- "CI on #42: success", "Time to: look at #3
    again" -- and the notice is where the model reads it; without this, every fired
    subscription would say "succeeded" and nothing else.
    """
    if record.kind is not Kind.subscription or not isinstance(payload, dict):
        return ""
    return str(payload.get("summary") or "")


def _due_line(record: Record, now: datetime) -> str:
    """For a check-in still waiting: how long until it is due, in the live block's words."""
    due = record.tags.get("due")
    if due is None or record.state.finished:
        return ""
    left = float(due) - now.timestamp()
    return "due now" if left < 1 else f"due in {_short(left)}"


def _short(seconds: float) -> str:
    whole = int(seconds)
    minutes, rest = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{rest:02d}s"
    return f"{rest}s"


def _expired(record: Record) -> str:
    """The sentence for a timeout, which means two different things for two kinds of work.

    A helper or a command that hit its ceiling may well still be running somewhere, and the
    person is owed that doubt. A watch that hit its ceiling simply never saw what it was
    waiting for, and what the person is owed is the offer to look again. So is a
    subscription: the sibling looked for its whole life and the condition never held.
    """
    if record.kind in {Kind.watch, Kind.subscription}:
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


async def _told_team(listener: TeamListener, team: Team) -> None:
    """One listener, one group ending. Logged like `_told`, by the group's name only."""
    try:
        await listener(team)
    except Exception as exc:
        logger.warning(
            "work team listener failed",
            extra={"group": team.group, "error": type(exc).__name__},
        )


async def _quietly(dropped: Awaitable[None], record: Record) -> None:
    """Tell whoever queued work that it was cancelled unstarted; a failure is only logged."""
    try:
        await dropped
    except Exception as exc:
        logger.warning(
            "work dropped callback failed",
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
    "CANCELLED_QUEUED",
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
