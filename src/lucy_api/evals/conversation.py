"""One scenario's session: send each turn, wait for it, answer what it asks, read it back.

Waiting is polling ``GET /v1/turns/{id}`` -- the route the hub documents for exactly this --
until the turn comes to rest: finished, parked on a person, or out of time. The event
stream would say the same thing sooner, but a poll cannot miss a frame, and a harness that
exists to be believed should not have a reconnect path to get wrong.

A parked turn is answered from the scenario's ``approve`` value, one approval at a time
(the hub refuses more than one per request), and polled again: approving a write resumes
the turn, and a turn can park more than once. ``ignore`` leaves it parked.

Out of time, the turn is cancelled, so an abandoned turn does not go on spending somebody's
model budget after the harness has stopped listening.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lucy_api.evals.checks import Observation, check_invocation, check_turn
from lucy_api.evals.direct import invoke
from lucy_api.evals.hub import HubError
from lucy_api.evals.results import TurnRecord
from lucy_api.evals.scenario import (
    AUTH_REQUIRED,
    IGNORE,
    INPUT_REQUIRED,
    LIFETIMES,
    NO,
    TERMINAL_STATUSES,
)
from lucy_api.evals.transcript import exchange_for, pending_approvals

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.evals.hub import Hub
    from lucy_api.evals.results import Check, InvocationRecord
    from lucy_api.evals.scenario import Invocation, TurnSpec

RESTING = frozenset({*TERMINAL_STATUSES, AUTH_REQUIRED})
"""States a turn will not leave without something the harness does not do."""

CACHE_READ = "turn_cache_read_tokens"


@dataclass(frozen=True, slots=True)
class Pace:
    """How the harness waits. Injected, so a test never sleeps."""

    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    poll_seconds: float = 1.0


class Transcript:
    """The session's items, read forward from a cursor so each page is fetched once."""

    def __init__(self, hub: Hub, session_id: str) -> None:
        self._hub = hub
        self._session_id = session_id
        self._after: str | None = None
        self.items: list[dict[str, Any]] = []

    def refresh(self) -> list[dict[str, Any]]:
        """Everything written so far, fetching only what is new."""
        while True:
            page = self._hub.items(self._session_id, self._after)
            data = page.get("data")
            rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
            self.items.extend(rows)
            last = page.get("last_id")
            if last:
                self._after = str(last)
            if not rows or not page.get("has_more"):
                return self.items


class Conversation:
    """A live session, and the turns this scenario left open in it."""

    def __init__(self, hub: Hub, session: dict[str, Any], *, pace: Pace | None = None) -> None:
        self.hub = hub
        self.session_id = str(session.get("id") or "")
        self.profile = str(session.get("profile") or "")
        self.pace = pace or Pace()
        self.log = Transcript(hub, self.session_id)
        self.usage: dict[str, Any] = {}
        self._open: set[str] = set()

    def invoke(self, invocation: Invocation) -> InvocationRecord:
        """A seed or verify step, in this session."""
        return invoke(self.hub, invocation, session_id=self.session_id, profile=self.profile)

    def take_turn(self, index: int, spec: TurnSpec, *, timeout: float) -> TurnRecord:
        """Say one thing, wait for the turn to come to rest, and check everything about it."""
        before = self._cache_read()
        started = self.pace.clock()
        sent = self.hub.send_message(self.session_id, spec.say)
        turn_id = str(sent.get("id") or "")
        turn, timed_out = self._settle(sent, turn_id, spec.approve, deadline=started + timeout)
        seconds = self.pace.clock() - started
        status = str(turn.get("status") or "")
        if status not in TERMINAL_STATUSES:
            self._open.add(turn_id)
        exchange = exchange_for(self.log.refresh(), turn_id)
        after = self._cache_read()
        seen = Observation(
            status=status,
            termination=str(turn.get("termination") or ""),
            seconds=seconds,
            timeout=timeout,
            timed_out=timed_out,
            exchange=exchange,
        )
        prefix = f"turn {index}: "
        checks: list[Check] = list(check_turn(spec.expect, seen, prefix=prefix))
        verified: list[InvocationRecord] = []
        for number, invocation in enumerate(spec.verify, start=1):
            record = self.invoke(invocation)
            verified.append(record)
            label = f"{prefix}verify {number} {invocation.op}: "
            checks.extend(check_invocation(invocation, record, prefix=label))
        return TurnRecord(
            index=index,
            said=spec.say,
            approve=spec.approve,
            turn_id=turn_id,
            status=status,
            termination=seen.termination,
            seconds=round(seconds, 3),
            timed_out=timed_out,
            iterations=_count(turn.get("iterations")),
            input_tokens=_count(turn.get("input_tokens")),
            output_tokens=_count(turn.get("output_tokens")),
            cache_read_tokens=after - before if after is not None and before is not None else None,
            reply=exchange.reply,
            results=exchange.results,
            asks=exchange.asks,
            errors=exchange.errors,
            verify=tuple(verified),
            checks=tuple(checks),
        )

    def close(self, *, keep: bool) -> tuple[str, ...]:
        """Cancel what this scenario left parked, then archive -- unless asked to keep it.

        Never raises: cleanup runs on the way out of a failure too, and a failure to tidy up
        must not replace the reason the scenario ended. What went wrong is returned instead.
        """
        if keep:
            return ()
        problems: list[str] = []
        for turn_id in sorted(self._open):
            try:
                self.hub.cancel_turn(turn_id)
            except HubError as exc:
                problems.append(f"could not cancel {turn_id}: {exc}")
        try:
            self.hub.archive(self.session_id)
        except HubError as exc:
            problems.append(f"could not archive {self.session_id}: {exc}")
        return tuple(problems)

    def _settle(
        self, turn: dict[str, Any], turn_id: str, approve: str, *, deadline: float
    ) -> tuple[dict[str, Any], bool]:
        """Poll until the turn rests, answering asks on the way. ``True`` if time ran out."""
        answered: set[str] = set()
        while True:
            status = str(turn.get("status") or "")
            if status in RESTING:
                return turn, False
            if status == INPUT_REQUIRED:
                pending = self._unanswered(turn_id, answered) if approve != IGNORE else ()
                if not pending:
                    return turn, False
                for approval_id in pending:
                    self.hub.answer_approval(
                        self.session_id,
                        approval_id,
                        approved=approve != NO,
                        lifetime=LIFETIMES[approve],
                    )
                    answered.add(approval_id)
                turn = self.hub.turn(turn_id)
                continue
            if self.pace.clock() >= deadline:
                return self.hub.cancel_turn(turn_id), True
            self.pace.sleep(self.pace.poll_seconds)
            turn = self.hub.turn(turn_id)

    def _unanswered(self, turn_id: str, answered: set[str]) -> tuple[str, ...]:
        waiting = pending_approvals(self.log.refresh(), turn_id)
        return tuple(approval_id for approval_id in waiting if approval_id not in answered)

    def _cache_read(self) -> int | None:
        """The session's cache-read total, which the turn row does not carry on its own."""
        self.usage = self.hub.usage(self.session_id)
        value = self.usage.get(CACHE_READ)
        return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["RESTING", "Conversation", "Pace", "Transcript"]
