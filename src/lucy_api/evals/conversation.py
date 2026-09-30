"""One scenario's session: send each turn, wait for it, answer what it asks, read it back.

Before a turn is sent, the steps its scenario lists under ``before`` are taken in order,
once the previous turn has come to rest: an operation through the invoke route, exactly as
a seed or verify step runs; a command on this machine, through the shell the conversation
was handed (:mod:`lucy_api.evals.host`; with none, every command is refused); or a pause.
The first that does not end as the scenario says leaves the turn unsent, because the
conversation after it would not be the one written down; the turn's record says why.

Waiting is polling ``GET /v1/turns/{id}`` -- the route the hub documents for exactly this --
until the turn comes to rest: finished, parked on a person, or out of time. The event
stream would say the same thing sooner, but a poll cannot miss a frame, and a harness that
exists to be believed should not have a reconnect path to get wrong.

A parked turn is answered from the scenario's ``approve`` value, one approval at a time
(the hub refuses more than one per request), and polled again: approving a write resumes
the turn, and a turn can park more than once. ``ignore`` leaves it parked.

Every poll also reads the turn's transcript and hands it to the watchdog
(:mod:`lucy_api.evals.watch`), which stops the turn at the first step that fails or the
first ask for a call already answered -- before the harness answers it again.

Out of time, the turn is cancelled, so an abandoned turn does not go on spending somebody's
model budget after the harness has stopped listening.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from lucy_api.evals.checks import (
    Observation,
    check_invocation,
    check_turn,
    invocation_failures,
)
from lucy_api.evals.direct import invoke
from lucy_api.evals.host import NOT_ALLOWED, REFUSED, failure
from lucy_api.evals.hub import HubError
from lucy_api.evals.results import BeforeRecord, TurnRecord
from lucy_api.evals.scenario import (
    AUTH_REQUIRED,
    FAILED,
    HOST_STEP,
    IGNORE,
    INPUT_REQUIRED,
    LIFETIMES,
    NO,
    OK,
    OP_STEP,
    TERMINAL_STATUSES,
    TURN_REFUSED,
    WAIT_STEP,
    HostCommand,
    Wait,
)
from lucy_api.evals.transcript import exchange_for, pending_approvals
from lucy_api.evals.watch import Watcher

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.evals.host import Shell
    from lucy_api.evals.hub import Hub
    from lucy_api.evals.results import Check, InvocationRecord
    from lucy_api.evals.scenario import BeforeStep, Invocation, TurnSpec
    from lucy_api.evals.watch import Halt

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

    def __init__(
        self,
        hub: Hub,
        session: dict[str, Any],
        *,
        pace: Pace | None = None,
        shell: Shell | None = None,
    ) -> None:
        self.hub = hub
        self.session_id = str(session.get("id") or "")
        self.profile = str(session.get("profile") or "")
        self.pace = pace or Pace()
        self.shell = shell
        self.log = Transcript(hub, self.session_id)
        self.usage: dict[str, Any] = {}
        self._open: set[str] = set()

    def invoke(self, invocation: Invocation) -> InvocationRecord:
        """A seed, before or verify step, in this session."""
        return invoke(self.hub, invocation, session_id=self.session_id, profile=self.profile)

    def take_turn(
        self,
        index: int,
        spec: TurnSpec,
        *,
        timeout: float,
        on_event: Callable[[str], None] | None = None,
    ) -> TurnRecord:
        """Take the steps before it, say one thing, wait for the turn to rest, check it all.

        ``on_event`` hears each step the harness takes before the turn as that step ends,
        then each step and ask of the turn the moment the transcript shows it.
        """
        taken, unsent = self._before(spec, on_event)
        if unsent:
            return _unsent(index, spec, taken, unsent)
        before = self._cache_read()
        started = self.pace.clock()
        try:
            sent = self.hub.send_message(self.session_id, spec.say)
        except HubError as exc:
            # A refusal the scenario expects is its answer, not a broken run; any other is
            # still the run stopping, as it always was.
            if not spec.expect.refused or exc.fatal or not exc.problem:
                raise
            seen = Observation(
                status=TURN_REFUSED,
                termination="",
                seconds=self.pace.clock() - started,
                timeout=timeout,
                timed_out=False,
                exchange=exchange_for([], ""),
                refused=exc.problem,
                refusal=str(exc),
            )
            return _refused(index, spec, taken, seen)
        turn_id = str(sent.get("id") or "")
        watcher = Watcher(
            turn_id,
            allowed=spec.expect.allow_errors,
            expecting_failure=spec.expect.status == FAILED,
            on_event=on_event,
        )
        turn, timed_out, halt = self._settle(
            sent, turn_id, spec.approve, deadline=started + timeout, watcher=watcher
        )
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
            halted=str(halt) if halt is not None else "",
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
            halted=seen.halted,
            before=taken,
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

    def _before(
        self, spec: TurnSpec, on_event: Callable[[str], None] | None
    ) -> tuple[tuple[BeforeRecord, ...], str]:
        """Each step before the turn, in order, until one does not end as the scenario says.

        Returns what every step taken did, and why the turn must not be sent: empty when it
        may be.
        """
        taken: list[BeforeRecord] = []
        for number, step in enumerate(spec.before, start=1):
            started = self.pace.clock()
            record, why = self._step(number, step)
            record = replace(record, seconds=round(self.pace.clock() - started, 3))
            taken.append(record)
            if on_event is not None:
                on_event(describe_before(record))
            if why:
                return tuple(taken), why
        return tuple(taken), ""

    def _step(self, number: int, step: BeforeStep) -> tuple[BeforeRecord, str]:
        """One step, and why it did not end as written: empty when it did."""
        if isinstance(step, Wait):
            self.pace.sleep(step.seconds)
            waited = BeforeRecord(kind=WAIT_STEP, step=f"{step.seconds:g}s", status=OK, passed=True)
            return waited, ""
        if isinstance(step, HostCommand):
            return self._command(number, step)
        ran = self.invoke(step)
        why = invocation_failures(step, ran, prefix=f"before {number} {step.op}: ")
        record = BeforeRecord(
            kind=OP_STEP,
            step=ran.op,
            status=ran.status,
            passed=not why,
            input=ran.input,
            output=ran.output,
            error=ran.error,
        )
        return record, why

    def _command(self, number: int, step: HostCommand) -> tuple[BeforeRecord, str]:
        """A command on this machine: it must exit 0 before its timeout."""
        label = f"before {number} `{step.command}`"
        if self.shell is None:
            refused = BeforeRecord(
                kind=HOST_STEP,
                step=step.command,
                status=REFUSED,
                passed=False,
                error=NOT_ALLOWED,
            )
            return refused, f"{label}: {NOT_ALLOWED}"
        finished = self.shell(step.command, step.timeout_seconds)
        passed = finished.status == OK
        record = BeforeRecord(
            kind=HOST_STEP,
            step=step.command,
            status=finished.status,
            passed=passed,
            output=finished.output,
        )
        return record, "" if passed else f"{label}: {failure(finished, step.timeout_seconds)}"

    def _settle(
        self,
        turn: dict[str, Any],
        turn_id: str,
        approve: str,
        *,
        deadline: float,
        watcher: Watcher,
    ) -> tuple[dict[str, Any], bool, Halt | None]:
        """Poll until the turn rests or goes wrong, answering asks on the way.

        Returns the turn, whether time ran out, and why the watchdog stopped it, if it did.
        The watchdog looks before anything is answered, so an ask for a call already given
        an answer is never answered twice.
        """
        answered: set[str] = set()
        while True:
            status = str(turn.get("status") or "")
            _, halt = watcher.look(self.log.refresh())
            if halt is not None:
                if status not in TERMINAL_STATUSES:
                    turn = self.hub.cancel_turn(turn_id)
                return turn, False, halt
            if status in RESTING:
                return turn, False, None
            if status == INPUT_REQUIRED:
                pending = self._unanswered(turn_id, answered) if approve != IGNORE else ()
                if not pending:
                    return turn, False, None
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
                return self.hub.cancel_turn(turn_id), True, None
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


def describe_before(record: BeforeRecord) -> str:
    """``> workspace.write -> ok``, ``> $ docker stop x -> ok in 10.4s``, ``> waited 5s``.

    Marked ``>``, because the harness took the step, not the model.
    """
    if record.kind == WAIT_STEP:
        return f"> waited {record.step}"
    if record.kind == HOST_STEP:
        return f"> $ {record.step} -> {record.status} in {record.seconds:.1f}s"
    return f"> {record.step} -> {record.status}"


def _unsent(index: int, spec: TurnSpec, taken: tuple[BeforeRecord, ...], why: str) -> TurnRecord:
    """A turn a before-step stopped: nothing was sent, so there is nothing to check."""
    return TurnRecord(
        index=index,
        said=spec.say,
        approve=spec.approve,
        turn_id="",
        status="",
        termination="",
        seconds=0.0,
        timed_out=False,
        iterations=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=None,
        reply="",
        results=(),
        asks=(),
        errors=(),
        verify=(),
        checks=(),
        before=taken,
        unsent=why,
    )


def _refused(
    index: int, spec: TurnSpec, taken: tuple[BeforeRecord, ...], seen: Observation
) -> TurnRecord:
    """A message the hub answered with a problem: no turn, no reply, and the checks on that."""
    return TurnRecord(
        index=index,
        said=spec.say,
        approve=spec.approve,
        turn_id="",
        status=TURN_REFUSED,
        termination="",
        seconds=round(seen.seconds, 3),
        timed_out=False,
        iterations=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=None,
        reply="",
        results=(),
        asks=(),
        errors=(),
        verify=(),
        checks=check_turn(spec.expect, seen, prefix=f"turn {index}: "),
        before=taken,
        refused=seen.refusal,
    )


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["RESTING", "Conversation", "Pace", "Transcript", "describe_before"]
