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

Work started in a named group wakes nothing on its own ending. The group's ending -- its last
member's -- is a `lucy.work.group.finished` event, and wakes the session once, with one line
naming every member and how each ended. Five reviewers are one piece of news, and five
wakes would open turns that each knew a fifth of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import TERMINAL
from lucy_api.sessions.sql_store import NewItem
from lucy_api.sessions.turns import open_turn
from lucy_api.stream.emitter import NewEvent
from lucy_api.stream.events import WORK_FINISHED, WORK_GROUP_FINISHED, WORK_WOKE
from lucy_api.work.types import Team

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.sessions.sql_store import SessionStore
    from lucy_api.stream.emitter import EventEmitter
    from lucy_api.work.types import Record

type Ending = Record | Team
"""What a wake is about: one piece of work, or a group whose last member has ended."""

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


def team_wake_line(team: Team) -> str:
    """What the model reads when a group's ending wakes it. The same standing as one ending."""
    return (
        f"[harness: {team.line()}. Nothing here is from the person. Read each result you need "
        "by its id, tell them what the group found, and carry on with anything it unblocks.]"
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
        self._held: dict[str, list[Ending]] = {}

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
        if not record.wake or record.group:
            # A member of a group is woken for by its group, once, when the last one ends.
            return
        await self._wake_or_hold(record, busy=busy)

    async def on_team_finished(self, team: Team) -> None:
        """The registry's group listener: announce the group's ending, then wake or hold."""
        busy = False
        if team.account_id:
            try:
                busy = await self._busy(team)
            except LucyError:
                return
        await self._events.emit(team.session_id, NewEvent(WORK_GROUP_FINISHED, _team(team)))
        if not team.wake:
            return
        await self._wake_or_hold(team, busy=busy)

    async def _wake_or_hold(self, ending: Ending, *, busy: bool) -> None:
        if busy:
            self._held.setdefault(ending.session_id, []).append(ending)
            return
        await self._open(ending)

    async def flush(self, session_id: str) -> None:
        """Spend the endings held while a turn was running, now that it is not.

        Called after every turn ends. A held ending whose result the turn already read is
        dropped rather than announced: the model has seen it, and a wake would be the same
        news twice -- for a group, when every member's result was read. If another turn has
        started in the meantime the rest stay held.
        """
        held = self._held.pop(session_id, [])
        for position, ending in enumerate(held):
            if ending.fetched:
                continue
            try:
                busy = await self._busy(ending)
            except LucyError:
                return
            if busy:
                self._held[session_id] = held[position:]
                return
            await self._open(ending)

    async def _busy(self, ending: Ending) -> bool:
        turns = await self._store.records(ending.account_id, ending.session_id, "turns")
        return any(str(turn["status"]) not in TERMINAL for turn in turns)

    async def _open(self, ending: Ending) -> None:
        if isinstance(ending, Team):
            wake: dict[str, Any] = {
                "type": WAKE_INPUT,
                "group": ending.group,
                "work_ids": ending.ids,
            }
            line = team_wake_line(ending)
            woke: dict[str, Any] = {"group": ending.group, "work_ids": list(ending.ids)}
        else:
            wake = {"type": WAKE_INPUT, "work_id": ending.id}
            line = wake_line(ending)
            woke = {"work_id": ending.id, "state": ending.state.value}
        turn = await open_turn(
            self._store, ending.account_id, ending.session_id, {"events": [wake]}
        )
        turn_id = str(turn["id"])
        await self._store.append(
            ending.account_id,
            ending.session_id,
            NewItem(NOTICE_KIND, NOTICE_ROLE, line, turn=turn_id),
        )
        await self._events.emit(ending.session_id, NewEvent(WORK_WOKE, woke, turn_id))
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
        **({"group": record.group} if record.group else {}),
    }


def _team(team: Team) -> dict[str, Any]:
    """The group event body: each member's ending and its size, never a payload."""
    return {
        "group": team.group,
        "members": [
            {
                "work_id": member.id,
                "role": member.role,
                "state": member.state.value,
                "result_tokens": member.tokens,
            }
            for member in team.members
        ],
        "wake": team.wake,
    }


__all__ = ["NOTICE_KIND", "NOTICE_ROLE", "WAKE_INPUT", "Waker", "team_wake_line", "wake_line"]
