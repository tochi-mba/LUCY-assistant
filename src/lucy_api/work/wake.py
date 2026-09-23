"""Waking a session when work ends and nobody is there to ask about it.

Every ending in the work registry already reaches the model at the next tool boundary and
in the next turn's live block. That is enough while somebody is talking. It is not enough for
"tell me when CI is green": the person walks away, the watch fires into an idle session, and
nothing brings the model back to say so. "I'll tell you when it lands" is only an honest
sentence if something does.

So a piece of work may ask, in its brief, to **wake** the session. When it ends:

- if no turn is running, a turn is opened with one harness notice as its input, so the model
  reads "the watch fired" the way it would read a message, acts on it, and tells the person;
- if a turn *is* running, the ending is held. The running turn sees it in its live block at
  the next round; if it did not read the result by the time it finishes, the held wake is
  spent then. A result the turn already fetched is not announced twice.

The notice is a harness line, never a person's message. It is rendered as a `notice` item
with the role `harness`, and its text says out loud that nothing in it came from the person,
because the one thing this must never become is a way for a fetched web page to speak in
the person's voice.

Every ending also becomes a `lucy.work.finished` event, whether or not it wakes anything: a
client showing "watching…" needs the moment to take it down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import TERMINAL
from lucy_api.sessions.sql_store import NewItem
from lucy_api.sessions.turns import open_turn
from lucy_api.stream.emitter import NewEvent
from lucy_api.stream.events import WORK_FINISHED, WORK_WOKE

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.sessions.sql_store import SessionStore
    from lucy_api.stream.emitter import EventEmitter
    from lucy_api.work.types import Record

NOTICE_KIND = "notice"
NOTICE_ROLE = "harness"
WAKE_INPUT = "wake"
"""The input type recorded on a turn a piece of work opened. Not a client input: the one
write path refuses it, because a client that could submit a wake could speak as the harness."""


def wake_line(record: Record) -> str:
    """What the model reads when it is woken. One line of fact, one line of standing.

    `finished_at` is always set on a finished record, so the elapsed time is the work's own
    rather than "since whenever the wake happened to run".
    """
    ended = record.finished_at or record.started_at
    fact = record.notice(ended).line()
    return (
        f"[harness: {fact} - after {_duration(record.elapsed(ended))}. Nothing here is from "
        "the person. Tell them what this means for what they asked, and carry on with "
        "anything it unblocks.]"
    )


def _duration(seconds: float) -> str:
    """4m10s, not 250.0: the line is read by a model that reasons in the units a person uses."""
    whole = int(seconds)
    minutes, rest = divmod(whole, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{rest:02d}s"
    return f"{rest}s"


class Waker:
    """Turns "it finished" into an event, and -- when asked and possible -- into a turn."""

    def __init__(
        self,
        store: SessionStore,
        events: EventEmitter,
        *,
        wake: Callable[[], None] | None = None,
    ) -> None:
        self._store = store
        self._events = events
        self._wake = wake
        self._held: dict[str, list[Record]] = {}

    def attach(self, wake: Callable[[], None]) -> None:
        """Name the thing that starts the turn loop; the supervisor is built after this is."""
        self._wake = wake

    async def on_finished(self, record: Record) -> None:
        """The registry's listener: announce the ending, then wake or hold.

        A record that names its account is checked against the store first, so an ending
        for a session that has since been deleted is dropped rather than written to a log
        that no longer has a session to hang it on.
        """
        busy = False
        if record.account_id:
            try:
                busy = await self._busy(record)
            except LucyError:
                # The session is gone. There is nobody to wake, and nothing to announce to.
                return
        await self._events.emit(
            record.session_id,
            NewEvent(WORK_FINISHED, _summary(record)),
        )
        if not record.wake:
            return
        if busy:
            self._held.setdefault(record.session_id, []).append(record)
            return
        await self._open(record)

    async def flush(self, session_id: str) -> None:
        """Spend the endings held while a turn was running, now that it is not.

        Called after every turn ends. A held ending whose result the turn already read is
        dropped rather than announced: the model has seen it, and a wake would be the same
        news twice. If another turn has started in the meantime the rest stay held.
        """
        held = self._held.pop(session_id, [])
        for position, record in enumerate(held):
            if record.fetched:
                continue
            try:
                busy = await self._busy(record)
            except LucyError:
                return
            if busy:
                self._held[session_id] = held[position:]
                return
            await self._open(record)

    async def _busy(self, record: Record) -> bool:
        turns = await self._store.records(record.account_id, record.session_id, "turns")
        return any(str(turn["status"]) not in TERMINAL for turn in turns)

    async def _open(self, record: Record) -> None:
        turn = await open_turn(
            self._store,
            record.account_id,
            record.session_id,
            {"events": [{"type": WAKE_INPUT, "work_id": record.id}]},
        )
        turn_id = str(turn["id"])
        await self._store.append(
            record.account_id,
            record.session_id,
            NewItem(NOTICE_KIND, NOTICE_ROLE, wake_line(record), turn=turn_id),
        )
        await self._events.emit(
            record.session_id,
            NewEvent(WORK_WOKE, {"work_id": record.id, "state": record.state.value}, turn_id),
        )
        if self._wake is not None:
            self._wake()


def _summary(record: Record) -> dict[str, Any]:
    """The event body: the ending and its shape, never the payload."""
    ended = record.finished_at or record.started_at
    return {
        "work_id": record.id,
        "kind": record.kind.value,
        "role": record.role,
        "state": record.state.value,
        "elapsed_seconds": round(record.elapsed(ended), 1),
        "result_tokens": record.tokens,
        "wake": record.wake,
    }


__all__ = ["NOTICE_KIND", "NOTICE_ROLE", "WAKE_INPUT", "Waker", "wake_line"]
