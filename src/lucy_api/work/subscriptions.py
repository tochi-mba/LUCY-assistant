"""Work a sibling finishes for Lucy: durable, and ended by a signed signal rather than a poll.

"Tell me when CI is green" is a condition only the service that can see CI can check well. A
watch (`work.watch`) has Lucy look again every few seconds for at most an hour, in this process,
and a restart forgets it. A **subscription** turns that around: the sibling looks, for as long
as the subscription lives, and when the condition holds it POSTs a short signed signal to
`/v1/signals/{id}`. Lucy ends the work, and the work registry does what it does for every
ending -- the notice at the next tool boundary, the line in the live block, and, when the work
asked, a turn opened on an idle session (`work.wake`).

Everything a person or a model sees is the registry's: a subscription has a handle, shows in
`work.check`, is cancelled with `work.cancel` and is read with `work.result`. What this module
adds is only what the registry cannot do on its own:

- **A durable row.** The registry holds records in memory. Each subscription is also a row, so
  the next process re-registers whatever was still open (`restore`) under the same work id,
  and a subscription that fired while Lucy was down is found by `sweep`, which asks the
  sibling directly.
- **A secret per subscription.** The sibling signs its signal with it, Lucy checks it in
  constant time (`net.signing`), and a signal that does not verify, names a subscription that
  does not exist, or names one that already ended is refused without saying which -- an
  outsider guessing ids learns nothing.
- **Release on cancel.** A person who cancels a subscription is done with it: the sibling is
  asked to stop looking and any standing consent it carried is withdrawn, through whatever
  the capability registered (`on_release`).

A signal carries *that* the condition held, with a one-line summary, a few small facts and a
bounded excerpt -- the `Check` shape a watch returns. It is never the result itself: the woken
turn reads that through the capability, which frames it as untrusted like any other result.

A **check-in** is a subscription the hub ends itself, at a time: "come back at 19:24 and look
at the pull request again". No sibling is involved. The row is the same row, so a restart
takes it up and a check-in that fell due while Lucy was down fires as soon as she is back,
saying how late it is; the ending is the same ending, so the notice, the wake and the standing
consent it carries are what every subscription gets. What differs is only who ends it: a timer
in this process rather than a signal from outside.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from lucy_api.core.errors import LucyError, absent, conflict
from lucy_api.net.signing import verify
from lucy_api.sessions.sql_store import encoded, identifier, row_value
from lucy_api.work.types import Brief, Handle, Kind, State, WorkError
from lucy_api.work.watch import clip

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Awaitable, Callable, Mapping

    from lucy_api.sessions.sql_store import SessionStore
    from lucy_api.work.registry import Registry
    from lucy_api.work.types import Record

logger = logging.getLogger(__name__)

SignalState = Literal["fired", "failed", "expired"]
SIGNAL_STATES: frozenset[str] = frozenset({"fired", "failed", "expired"})

MAX_SIGNAL_BYTES = 8 * 1024
"""How large a signal may be. It says that something happened, not what: a summary, a few
small facts and an excerpt fit in far less, and a larger body is refused before it is read."""

MAX_SUMMARY = 120
MAX_FACTS = 12
MAX_FACT_CHARS = 120

MAX_LIFETIME_SECONDS = 7 * 24 * 3600.0
"""How long a subscription may live: a week. Longer is a standing job somebody should set up
on purpose, not something a conversation leaves behind."""

DEFAULT_SWEEP_SECONDS = 120.0

CHECKIN_CAPABILITY = "work"
"""The capability a check-in belongs to: the hub's own bookkeeping, not a sibling's."""

CHECKIN_ROLE = "check-in"
"""What a check-in is called in a notice and the live block, where `work` would say nothing."""

CHECKIN_GRACE_SECONDS = 900.0
"""How long after its due time a check-in may still fire before it is given up as missed. A
timer in a healthy process fires within a second of due; this is the net under a process
that was down across the due time, and the length of the "late" a restart may announce."""

MIN_CHECKIN_SECONDS = 60.0
"""Sooner than this is not a check-in but a wait, and `work.wait` is for waiting."""

TOO_SOON = f"A check-in is at least {MIN_CHECKIN_SECONDS:.0f} seconds away; to wait less, wait."
TOO_FAR = f"A check-in is at most {MAX_LIFETIME_SECONDS / 86400:.0f} days away."
CHECKIN_TOO_SOON = "checkin-too-soon"
CHECKIN_TOO_FAR = "checkin-too-far"

ENDED = "This subscription has already ended."
MALFORMED = (
    "The signal must be a JSON object with `state` (fired, failed or expired), `summary` (one "
    "line), and optionally `facts` (an object of short values) and `excerpt`."
)
INVALID_SIGNAL = "invalid-signal"
TOO_LARGE = "The signal is larger than Lucy accepts; send what happened, not the result."

type Release = Callable[[Mapping[str, Any]], Awaitable[None]]
"""What a capability does when a person cancels one of its subscriptions: ask its sibling to
stop looking, and withdraw any standing consent. Best effort; a failure is logged by type."""

type Check = Callable[[Mapping[str, Any]], Awaitable["Signal | None"]]
"""How the sweep asks a capability's sibling whether an open subscription has ended: a signal
when it has, ``None`` while it has not."""


@dataclass(frozen=True, slots=True)
class Signal:
    """What a sibling says when a subscription ends. Small on purpose; see the module doc."""

    state: SignalState
    summary: str
    facts: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)
    excerpt: str = ""

    def payload(self) -> dict[str, Any]:
        """What `work.result` returns: that it ended and how, never more than this."""
        return {
            "state": self.state,
            "summary": self.summary,
            **({"facts": dict(self.facts)} if self.facts else {}),
            **({"excerpt": self.excerpt} if self.excerpt else {}),
        }


@dataclass(frozen=True, slots=True)
class Opened:
    """A subscription Lucy has recorded and is waiting on, and what the sibling needs."""

    handle: Handle
    subscription_id: str
    secret: str
    """Handed to the sibling once, to sign its signal with. Never logged, never shown."""
    signal_url: str

    def __repr__(self) -> str:
        """The secret is absent on purpose: a repr is what a traceback prints."""
        return f"Opened(subscription_id={self.subscription_id!r}, signal_url={self.signal_url!r})"


class Subscriptions:
    """Every open subscription, as rows and as running records in the work registry."""

    def __init__(
        self,
        store: SessionStore,
        registry: Registry,
        *,
        signal_base_url: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._registry = registry
        self._signal_base_url = signal_base_url.rstrip("/")
        self._clock = clock
        self._sleep = sleep
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._waiting: dict[str, asyncio.Future[Signal]] = {}
        self._releases: dict[str, Release] = {}
        self._checks: dict[str, Check] = {}
        self._let_go: Release | None = None
        registry.on_finished(self._finished)

    def on_release(self, capability: str, release: Release) -> None:
        """Register how one capability lets go of a subscription a person cancelled."""
        self._releases[capability] = release

    def on_consent_release(self, release: Release) -> None:
        """Register how standing consent is withdrawn when any subscription is cancelled."""
        self._let_go = release

    def on_check(self, capability: str, check: Check) -> None:
        """Register how the sweep asks one capability's sibling about an open subscription."""
        self._checks[capability] = check

    # ---------------------------------------------------------------- opening

    async def open(  # noqa: PLR0913 - who, where, what, for how long, and under what consent
        self,
        *,
        account_id: str,
        session_id: str,
        profile: str,
        capability: str,
        objective: str,
        timeout_seconds: float,
        wake: bool = True,
        grant_id: str = "",
        role: str = "",
        due_at: float | None = None,
    ) -> Opened:
        """Record a subscription and start waiting on it. The sibling is told separately.

        The row is written before the record is started, so a signal that arrives before this
        returns -- a fast sibling -- finds a row to end. One with a `due_at` is a check-in:
        a timer in this process ends it then, and nothing outside is told.
        """
        lifetime = max(1.0, min(float(timeout_seconds), MAX_LIFETIME_SECONDS))
        subscription_id = identifier("sub")
        work_id = identifier("wrk")
        secret = secrets.token_urlsafe(32)
        now = self._clock()

        def apply(db: sqlite3.Connection) -> None:
            db.execute(
                "INSERT INTO subscriptions (id, account_id, session_id, profile, work_id, "
                "capability, sibling_id, secret, grant_id, objective, wake, state, created_at, "
                "expires_at, ended_at, result_json, due_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    subscription_id,
                    account_id,
                    session_id,
                    profile,
                    work_id,
                    capability,
                    None,
                    secret,
                    grant_id or None,
                    objective,
                    int(wake),
                    State.running.value,
                    now,
                    now + lifetime,
                    None,
                    None,
                    due_at,
                ),
            )

        await self._store.transaction(apply)
        handle = self._start(
            subscription_id,
            work_id=work_id,
            brief=Brief(
                session_id=session_id,
                kind=Kind.subscription,
                role=role or capability,
                objective=objective,
                timeout_seconds=lifetime,
                account_id=account_id,
                wake=wake,
                tags=_tags(capability, subscription_id, grant_id, due_at),
            ),
        )
        if due_at is not None:
            self._arm(subscription_id, due_at)
        return Opened(
            handle=handle,
            subscription_id=subscription_id,
            secret=secret,
            signal_url=f"{self._signal_base_url}/{subscription_id}",
        )

    async def attach(self, subscription_id: str, sibling_id: str) -> None:
        """Remember the sibling's own id for this subscription, so it can be released."""

        def apply(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE subscriptions SET sibling_id=? WHERE id=?", (sibling_id, subscription_id)
            )

        await self._store.transaction(apply)

    def abandon(self, opened: Opened) -> None:
        """End a subscription nobody will signal, as cancelled, releasing what it holds."""
        self._registry.cancel(opened.handle.id)

    async def checkin(  # noqa: PLR0913 - who, where, what, when, and under what consent
        self,
        *,
        account_id: str,
        session_id: str,
        profile: str,
        objective: str,
        due_at: float,
        grant_id: str = "",
    ) -> Opened:
        """Open a check-in: a subscription this process ends at `due_at`, waking the session.

        Raises:
            LucyError: 400 when the time is sooner than a check-in is for, or further away
                than one may live. The message names the bound.
        """
        delay = due_at - self._clock()
        if delay < MIN_CHECKIN_SECONDS:
            raise LucyError(CHECKIN_TOO_SOON, TOO_SOON, 400)
        if delay > MAX_LIFETIME_SECONDS:
            raise LucyError(CHECKIN_TOO_FAR, TOO_FAR, 400)
        return await self.open(
            account_id=account_id,
            session_id=session_id,
            profile=profile,
            capability=CHECKIN_CAPABILITY,
            objective=objective,
            timeout_seconds=delay + CHECKIN_GRACE_SECONDS,
            wake=True,
            grant_id=grant_id,
            role=CHECKIN_ROLE,
            due_at=due_at,
        )

    def _start(self, subscription_id: str, *, work_id: str, brief: Brief) -> Handle:
        waiting: asyncio.Future[Signal] = asyncio.get_running_loop().create_future()
        self._waiting[subscription_id] = waiting
        return self._registry.start(_until(waiting), brief, work_id=work_id)

    # ---------------------------------------------------------------- check-in timers

    def _arm(self, subscription_id: str, due_at: float) -> None:
        """Start the timer that ends a check-in when it falls due."""
        self._timers[subscription_id] = asyncio.get_running_loop().create_task(
            self._fire(subscription_id, due_at), name=f"checkin:{subscription_id}"
        )

    async def _fire(self, subscription_id: str, due_at: float) -> None:
        """Sleep until due, then end the check-in as fired, saying how late it is if at all.

        A check-in whose row ended first -- cancelled, or taken up and fired by another path
        -- is a conflict here and nothing else: the row's ending was the whole ending.
        """
        await self._sleep(max(0.0, due_at - self._clock()))
        now = self._clock()
        late = max(0, round(now - due_at))
        try:
            await self._end(subscription_id, checkin_signal(due_at, now, late))
        except LucyError:
            logger.info("checkin_already_ended", extra={"subscription_id": subscription_id})

    def _disarm(self, subscription_id: str) -> None:
        """Forget a check-in's timer, cancelling it unless it is the one running now."""
        timer = self._timers.pop(subscription_id, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()

    async def aclose(self) -> None:
        """Stop every timer. The rows stay open; the next process arms them again."""
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            with contextlib.suppress(asyncio.CancelledError):
                await timer

    # ---------------------------------------------------------------- ending

    async def signal(self, subscription_id: str, signature: str | None, body: bytes) -> None:
        """End a subscription from the sibling's signed signal.

        Raises:
            LucyError: 413 for a body over the cap; 404 for a subscription that does not
                exist or a signature that does not verify, which are deliberately the same
                answer; 409 for one that already ended; 422 for a verified body that is not a
                signal.
        """
        if len(body) > MAX_SIGNAL_BYTES:
            code = "signal-too-large"
            raise LucyError(code, TOO_LARGE, 413)
        row = await self._row(subscription_id)
        if row is None or not verify(str(row["secret"]), signature, body):
            raise absent()
        await self._end(subscription_id, parse_signal(body))

    async def _end(self, subscription_id: str, signal: Signal) -> None:
        """Mark the row ended and resolve the waiting record, once."""
        now = self._clock()

        def apply(db: sqlite3.Connection) -> bool:
            ended = db.execute(
                "UPDATE subscriptions SET state=?, ended_at=?, result_json=? "
                "WHERE id=? AND state=?",
                (
                    _state_for(signal).value,
                    now,
                    encoded(signal.payload()),
                    subscription_id,
                    State.running.value,
                ),
            )
            return ended.rowcount == 1

        if not await self._store.transaction(apply):
            raise conflict(ENDED)
        self._disarm(subscription_id)
        # Only this method resolves a waiting future, and it pops it first, so one that is
        # here has not been resolved. One that is not here belongs to a row this process
        # has not taken up yet (`restore`): the row is ended, and that is the whole ending.
        waiting = self._waiting.pop(subscription_id, None)
        if waiting is not None:
            waiting.set_result(signal)

    async def _finished(self, record: Record) -> None:
        """Keep the row in step with an ending the registry decided: a cancel or an expiry.

        A signal has already ended its row, so this only touches rows still running. A cancel
        is the person letting go, so the capability is asked to release what it holds.
        """
        if record.kind is not Kind.subscription:
            return
        subscription_id = record.tags.get("subscription", "")
        self._waiting.pop(subscription_id, None)
        self._disarm(subscription_id)
        now = self._clock()

        def apply(db: sqlite3.Connection) -> dict[str, Any] | None:
            ended = db.execute(
                "UPDATE subscriptions SET state=?, ended_at=? WHERE id=? AND state=?",
                (record.state.value, now, subscription_id, State.running.value),
            )
            if ended.rowcount != 1:
                return None
            row = db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,))
            return row_value(row.fetchone())

        row = await self._store.transaction(apply)
        if row is None or record.state is not State.cancelled:
            return
        releases = [self._releases.get(str(row["capability"]))]
        if row.get("grant_id"):
            releases.append(self._let_go)
        for release in releases:
            if release is None:
                continue
            try:
                await release(row)
            except Exception as exc:
                logger.info(
                    "subscription_release_failed",
                    extra={"subscription_id": subscription_id, "error": type(exc).__name__},
                )

    # ---------------------------------------------------------------- after a restart

    async def restore(self) -> int:
        """Re-register every subscription a previous process left open. Returns how many.

        Each comes back under its own work id with what is left of its lifetime, so the handle
        the model was given still works. One already past its deadline is registered with a
        moment to live, so it ends `timed_out` and is told like any other expiry -- except a
        check-in, which is given its grace again and fires at once, saying how late it is: a
        person who asked to be told at nine is owed "it is ten past, and here I am" rather
        than silence.
        """
        rows = await self.open_rows()
        now = self._clock()
        for row in rows:
            remaining = max(0.01, float(row["expires_at"]) - now)
            subscription_id = str(row["id"])
            due_at = row.get("due_at")
            due = float(due_at) if due_at is not None else None
            if due is not None:
                remaining = max(remaining, CHECKIN_GRACE_SECONDS)
            self._start(
                subscription_id,
                work_id=str(row["work_id"]),
                brief=Brief(
                    session_id=str(row["session_id"]),
                    kind=Kind.subscription,
                    role=CHECKIN_ROLE if due is not None else str(row["capability"]),
                    objective=str(row["objective"]),
                    timeout_seconds=remaining,
                    account_id=str(row["account_id"]),
                    wake=bool(row["wake"]),
                    tags=_tags(
                        str(row["capability"]), subscription_id, str(row["grant_id"] or ""), due
                    ),
                ),
            )
            if due is not None:
                self._arm(subscription_id, due)
        return len(rows)

    async def open_rows(self) -> list[dict[str, Any]]:
        """Every subscription still running, oldest first."""

        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = db.execute(
                "SELECT * FROM subscriptions WHERE state=? ORDER BY created_at, id",
                (State.running.value,),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self._store.worker.call(read)

    async def sweep(self) -> int:
        """Ask each open subscription's sibling whether it ended. Returns how many had.

        For the signal that was lost: sent while Lucy was restarting, or dropped on the way.
        A sibling that cannot be asked this time is asked again next time.
        """
        ended = 0
        for row in await self.open_rows():
            check = self._checks.get(str(row["capability"]))
            if check is None or not row.get("sibling_id"):
                continue
            try:
                signal = await check(row)
            except Exception as exc:
                logger.info(
                    "subscription_sweep_failed",
                    extra={"subscription_id": row["id"], "error": type(exc).__name__},
                )
                continue
            if signal is None:
                continue
            try:
                await self._end(str(row["id"]), signal)
            except LucyError:
                continue
            ended += 1
        return ended

    async def run_sweeps(self, every_seconds: float = DEFAULT_SWEEP_SECONDS) -> None:
        """Sweep on an interval until cancelled. The container starts and stops this."""
        while True:
            await asyncio.sleep(every_seconds)
            await self.sweep()

    async def _row(self, subscription_id: str) -> dict[str, Any] | None:
        def read(db: sqlite3.Connection) -> dict[str, Any] | None:
            row = db.execute(
                "SELECT * FROM subscriptions WHERE id=?", (subscription_id,)
            ).fetchone()
            return row_value(row) if row is not None else None

        row = await self._store.worker.call(read)
        if row is not None and row["state"] != State.running.value:
            raise conflict(ENDED)
        return row


type Consent = Callable[[float], Awaitable[str]]
"""Record standing consent for a lifetime in seconds, returning the grant's handle. Built per
request, closing over the person's token, so nothing that receives it ever holds the token."""

STANDING_MARGIN_SECONDS = 900
"""How long consent outlives its subscription: long enough for the turn the ending opens to
finish what it was asked to do, and no longer."""


class SubscriptionSeam:
    """What one turn's capabilities use to open subscriptions: who, where, and consent.

    A capability names what it is waiting for; the account, the session and the profile come
    from the turn, and so does consent. A subscription that will wake the session asks for
    standing consent to act for the person until it ends -- a grant they can see and revoke
    in keyring -- so the turn it opens can do what they asked ("merge it when CI is green")
    with nobody present. If consent cannot be recorded the subscription still opens, and the
    woken turn is told it has none and must ask.
    """

    def __init__(
        self,
        subscriptions: Subscriptions,
        *,
        account_id: str,
        session_id: str,
        profile: str,
        consent: Consent | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._account_id = account_id
        self._session_id = session_id
        self._profile = profile
        self._consent = consent

    async def open(
        self, *, capability: str, objective: str, timeout_seconds: float, wake: bool = True
    ) -> Opened:
        """Open one subscription for this turn's person and session."""
        grant_id = await self._consented(timeout_seconds) if wake else ""
        return await self._subscriptions.open(
            account_id=self._account_id,
            session_id=self._session_id,
            profile=self._profile,
            capability=capability,
            objective=objective,
            timeout_seconds=timeout_seconds,
            wake=wake,
            grant_id=grant_id,
        )

    async def checkin(self, *, objective: str, due_at: float, delay_seconds: float) -> Opened:
        """Open a check-in at `due_at` for this turn's person and session, with consent.

        `delay_seconds` is how far away that is by the caller's clock; consent is recorded
        for that long plus the grace a late check-in gets, so the turn it opens can act.
        """
        grant_id = await self._consented(delay_seconds + CHECKIN_GRACE_SECONDS)
        return await self._subscriptions.checkin(
            account_id=self._account_id,
            session_id=self._session_id,
            profile=self._profile,
            objective=objective,
            due_at=due_at,
            grant_id=grant_id,
        )

    async def _consented(self, timeout_seconds: float) -> str:
        """Record standing consent for a lifetime, or ``""`` when there is none to record."""
        if self._consent is None:
            return ""
        lifetime = min(float(timeout_seconds), MAX_LIFETIME_SECONDS) + STANDING_MARGIN_SECONDS
        try:
            return await self._consent(lifetime)
        except Exception as exc:
            logger.info("standing_consent_failed", extra={"error": type(exc).__name__})
            return ""

    async def attach(self, opened: Opened, sibling_id: str) -> None:
        """Record the sibling's id for a subscription it accepted."""
        await self._subscriptions.attach(opened.subscription_id, sibling_id)

    def abandon(self, opened: Opened) -> None:
        """Give up on a subscription the sibling never accepted: it ends cancelled."""
        self._subscriptions.abandon(opened)


def parse_signal(body: bytes) -> Signal:
    """A signal from its JSON body, or a 422 saying what a signal looks like."""
    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise _invalid() from exc
    if not isinstance(raw, dict):
        raise _invalid()
    state = raw.get("state")
    summary = raw.get("summary")
    facts = raw.get("facts", {})
    excerpt = raw.get("excerpt", "")
    if (
        state not in SIGNAL_STATES
        or not isinstance(summary, str)
        or not isinstance(facts, dict)
        or not isinstance(excerpt, str)
        or set(raw) - {"state", "summary", "facts", "excerpt"}
    ):
        raise _invalid()
    return Signal(
        state=state,
        summary=" ".join(summary.split())[:MAX_SUMMARY],
        facts=_small_facts(facts),
        excerpt=clip(excerpt),
    )


def _invalid() -> LucyError:
    return LucyError(INVALID_SIGNAL, MALFORMED, 422)


def _small_facts(facts: Mapping[str, object]) -> dict[str, str | int | float | bool | None]:
    """At most twelve facts, each a scalar; strings are clipped. Anything else is dropped."""
    kept: dict[str, str | int | float | bool | None] = {}
    for key, value in list(facts.items())[:MAX_FACTS]:
        name = str(key)[:MAX_FACT_CHARS]
        if isinstance(value, str):
            kept[name] = value[:MAX_FACT_CHARS]
        elif value is None or isinstance(value, bool | int | float):
            kept[name] = value
    return kept


def _state_for(signal: Signal) -> State:
    return {
        "fired": State.succeeded,
        "failed": State.failed,
        "expired": State.timed_out,
    }[signal.state]


async def _until(waiting: asyncio.Future[Signal]) -> dict[str, Any]:
    """The work a subscription's record runs: wait for the signal, end as it says."""
    signal = await waiting
    if signal.state == "failed":
        raise WorkError(signal.summary, payload=signal.payload())
    if signal.state == "expired":
        raise TimeoutError(signal.summary)
    return signal.payload()


def checkin_signal(due_at: float, fired_at: float, late_seconds: int) -> Signal:
    """What a check-in says when it fires: that it is time, and whether it is late.

    The objective is already on the record and in every line about it, so the summary says
    only what is new: that the moment has come, and -- after a restart that crossed it --
    how long ago it came.
    """
    summary = "It is time" if late_seconds < 1 else f"It was time {late_seconds}s ago"
    return Signal(
        state="fired",
        summary=summary,
        facts={
            "due_at": _iso(due_at),
            "fired_at": _iso(fired_at),
            "late_seconds": late_seconds,
        },
    )


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat(timespec="seconds")


def _tags(
    capability: str, subscription_id: str, grant_id: str, due_at: float | None = None
) -> dict[str, str]:
    tags = {"capability": capability, "subscription": subscription_id}
    if grant_id:
        tags["grant"] = grant_id
    if due_at is not None:
        tags["due"] = repr(float(due_at))
    return tags


__all__ = [
    "CHECKIN_CAPABILITY",
    "CHECKIN_GRACE_SECONDS",
    "CHECKIN_ROLE",
    "DEFAULT_SWEEP_SECONDS",
    "ENDED",
    "MALFORMED",
    "MAX_LIFETIME_SECONDS",
    "MAX_SIGNAL_BYTES",
    "MIN_CHECKIN_SECONDS",
    "STANDING_MARGIN_SECONDS",
    "TOO_FAR",
    "TOO_LARGE",
    "TOO_SOON",
    "Opened",
    "Signal",
    "SubscriptionSeam",
    "Subscriptions",
    "checkin_signal",
    "parse_signal",
]
