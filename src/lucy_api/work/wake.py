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

Two of the person's settings ride on the work, as tags its opener wrote from the turn that
started it, because nobody is present to read settings when it ends:

- **Quiet hours** (`lucy.quiet_hours`). A wake that falls inside the window is held back as a
  check-in due when it closes (`Subscriptions.defer_wake`). The ending's event goes out at
  once, as always; only the turn waits. The check-in is a row, so a restart keeps the
  promise; while this process lives it also remembers which ending it stands for, and tells
  that one -- or nothing, if the person has read the result in the meantime.
- **Consent withheld** (`lucy.act_unattended` off). The turn is told it runs without
  standing consent, the same sentence as consent that no longer works.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import TERMINAL
from lucy_api.sessions.sql_store import NewItem
from lucy_api.sessions.turns import open_turn
from lucy_api.stream.emitter import NewEvent
from lucy_api.stream.events import WORK_FINISHED, WORK_GROUP_FINISHED, WORK_WOKE
from lucy_api.work.quiet import NEAR_SECONDS, QUIET_TAG, QuietHours
from lucy_api.work.types import Record, State, Team

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from lucy_api.sessions.sql_store import SessionStore
    from lucy_api.stream.emitter import EventEmitter

type Ending = Record | Team
"""What a wake is about: one piece of work, or a group whose last member has ended."""

NOTICE_KIND = "notice"
NOTICE_ROLE = "harness"
WAKE_INPUT = "wake"
"""The input type recorded on a turn a piece of work opened. Not a client input: the one
write path refuses it, because a client that could submit a wake could speak as the harness."""

CONSENT_TAG = "consent"
WITHHELD = "withheld"
"""`consent: withheld` marks work opened for somebody who said a turn Lucy opens on her own
may only report (`lucy.act_unattended` off). No grant was asked for, and the turn is told so."""

type Defer = Callable[[Ending, float], Awaitable[str | None]]
"""Hold a wake back until a moment, as durable work of its own: its id, or ``None`` when it
could not be recorded and the wake must happen now rather than never."""


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


STANDING = (
    " It runs with the standing consent the person gave when they asked: act on what they "
    "asked for, and nothing more."
)
"""Said when a woken turn carries the person's standing consent, so the model knows it may
finish the job -- and that the job, not a new one, is what the consent covers."""

NO_STANDING = (
    " It runs without the person's standing consent, so it cannot act for them: say what "
    "happened and ask them before doing anything on their behalf."
)
"""Said when work that asked to act for the person has no usable consent: none was
recorded, or it was revoked or expired. The model can still tell them what happened."""


class Authority(Protocol):
    """How a woken turn gets the authority the work was opened with, when it carries any.

    `prepare` runs before the turn exists and answers with whatever the turn needs to act --
    opaque here -- or ``None`` when the ending carries no consent or its consent no longer
    works. `authorize` hands that to the turn once it has an id. The waker knows nothing about
    tokens; the composition root does.
    """

    def carries(self, ending: Ending) -> bool:
        """Whether this ending was opened with standing consent at all."""
        ...

    async def prepare(self, ending: Ending) -> object | None:
        """The authority for the turn this ending opens, or ``None``."""
        ...

    def authorize(self, turn_id: str, prepared: object) -> None:
        """Give the opened turn what `prepare` made, before anything claims it."""
        ...


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

    def __init__(  # noqa: PLR0913 - where, what to announce on, and four late-bound collaborators
        self,
        store: SessionStore,
        events: EventEmitter,
        *,
        wake: Callable[[], None] | None = None,
        authority: Authority | None = None,
        defer: Defer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._events = events
        self._wake = wake
        self._authority = authority
        self._defer = defer
        self._clock = clock
        self._held: dict[str, list[Ending]] = {}
        self._deferred: dict[str, Ending] = {}
        """Endings held back for quiet hours, by the id of the check-in that will tell them."""

    def attach(
        self,
        wake: Callable[[], None],
        authority: Authority | None = None,
        defer: Defer | None = None,
    ) -> None:
        """Name the thing that starts the turn loop, what lends a woken turn authority, and
        what holds a wake back for quiet hours.

        All are built after the waker: the supervisor needs it, and authority needs both.
        """
        self._wake = wake
        if authority is not None:
            self._authority = authority
        if defer is not None:
            self._defer = defer

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
        held_back = self._deferred.pop(record.id, None)
        if held_back is not None:
            # The check-in that stood in for a wake during quiet hours. It is told as the
            # ending it stood for -- unless the person called it off, or has already read it.
            if record.state is not State.cancelled and not held_back.fetched:
                await self._wake_or_hold(held_back, busy=busy)
            return
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
        await self._deliver(ending)

    async def _deliver(self, ending: Ending) -> None:
        """Open the turn now, or -- inside the person's quiet hours -- when they end."""
        until = quiet_until(ending, self._clock())
        if until is not None and self._defer is not None:
            standing_in = await self._defer(ending, until)
            if standing_in is not None:
                self._deferred[standing_in] = ending
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
            await self._deliver(ending)

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
        prepared: object | None = None
        if self._authority is not None and self._authority.carries(ending):
            prepared = await self._authority.prepare(ending)
            line = line[:-1] + (STANDING if prepared is not None else NO_STANDING) + "]"
        elif _withheld(ending):
            line = line[:-1] + NO_STANDING + "]"
        turn = await open_turn(
            self._store, ending.account_id, ending.session_id, {"events": [wake]}
        )
        turn_id = str(turn["id"])
        if self._authority is not None and prepared is not None:
            # Before anything else awaits: the supervisor may claim a queued turn at any
            # yield, and a woken turn claimed without its authority runs without it.
            self._authority.authorize(turn_id, prepared)
        await self._store.append(
            ending.account_id,
            ending.session_id,
            NewItem(NOTICE_KIND, NOTICE_ROLE, line, turn=turn_id),
        )
        await self._events.emit(ending.session_id, NewEvent(WORK_WOKE, woke, turn_id))
        if self._wake is not None:
            self._wake()


def quiet_until(ending: Ending, now: float) -> float | None:
    """When the quiet hours this ending was opened under close, if `now` is inside them.

    ``None`` when it carries no window, `now` is outside it, or the window closes within
    `NEAR_SECONDS`. A group is quiet when any member carries a window: they were started
    by the same person, in the same turn, under the same settings.
    """
    members = ending.members if isinstance(ending, Team) else (ending,)
    tag = next((member.tags[QUIET_TAG] for member in members if QUIET_TAG in member.tags), "")
    quiet = QuietHours.from_tag(tag)
    closes = quiet.ends_at(now) if quiet is not None else None
    if closes is None or closes - now <= NEAR_SECONDS:
        return None
    return closes


def _withheld(ending: Ending) -> bool:
    return isinstance(ending, Record) and ending.tags.get(CONSENT_TAG) == WITHHELD


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


__all__ = [
    "CONSENT_TAG",
    "NOTICE_KIND",
    "NOTICE_ROLE",
    "NO_STANDING",
    "STANDING",
    "WAKE_INPUT",
    "WITHHELD",
    "Authority",
    "Defer",
    "Waker",
    "quiet_until",
    "team_wake_line",
    "wake_line",
]
