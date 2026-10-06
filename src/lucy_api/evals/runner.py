"""Scenarios times models times repeats, one session each, one after another.

**Sequential, on purpose.** The hub is one process with one turn supervisor, so two
conversations at once measure contention as much as the model, and latency is one of the
things recorded. Every job is independent -- its own session, nothing shared but the hub --
and :meth:`Runner.run_job` takes one job and returns one record, so a later ``--parallel``
is a different loop around the same call rather than a rewrite.

**Four outcomes, kept apart.** ``passed`` and ``failed`` are verdicts about the model and
the hub. ``skipped`` means a capability the scenario needs is not ready, so it never
started and cost nothing. ``error`` means the harness could not hold the conversation at
all. Folding the last two into ``failed`` would report an unconnected capability as a
regression.

**One fatal error stops the run.** A hub that has gone away or stopped accepting the token
will fail every remaining scenario for the same reason; the run stops, says why, and the
report records every scenario it did not get to as ``error`` rather than dropping them.

**Nothing of the person's is changed.** Sessions are created with the scenario's own
permission mode, archived afterwards, and grants the harness needs are scoped to the one
session they are for (see :mod:`lucy_api.evals.direct`). A scenario's ``host`` commands run
on this machine only through a shell the runner was handed, and ``lucy eval run`` hands it
one only under ``--allow-host`` (see :mod:`lucy_api.evals.host`).
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.evals.checks import invocation_failures
from lucy_api.evals.conversation import Conversation, Pace
from lucy_api.evals.hub import HubError
from lucy_api.evals.results import ERROR, SKIPPED, ScenarioRecord, outcome_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.evals.host import Shell
    from lucy_api.evals.hub import Hub
    from lucy_api.evals.results import InvocationRecord, TurnRecord
    from lucy_api.evals.scenario import Scenario

DEFAULT_PROFILE = "personal"
DEFAULT_TIMEOUT = 300.0
"""Seconds a turn may take before it is cancelled. Real turns measured 12-35 s on one
round-trip model; a multi-round build on a weak one takes minutes, not hours."""

INPUT_POLICY = "enqueue"
"""Always sent: a scenario that leaves a turn parked and then says something else needs the
second message to queue, and a person's own default may be ``reject``."""

TITLE = "[eval] {name}"


@dataclass(frozen=True, slots=True)
class Job:
    """One scenario, one model, one repeat (counted from 1)."""

    scenario: Scenario
    model: str
    repeat: int
    profile: str = DEFAULT_PROFILE


@dataclass(frozen=True, slots=True)
class Plan:
    """What a run will do, decided in full before anything is created."""

    scenarios: tuple[Scenario, ...]
    models: tuple[str, ...]
    repeat: int = 1
    profile: str = DEFAULT_PROFILE
    timeout: float = DEFAULT_TIMEOUT
    keep_sessions: bool = False
    apart: bool = False
    """Each conversation in a profile of its own, `<profile>-<n>`, rather than all in one.

    One profile per run still shared memory between the run's own conversations: the tea a
    scenario asked Lucy to remember sat beside the review day another asked about, and a
    repeat found the fact its predecessor saved and corrected it instead of saving it --
    failing for the run before, not for anything it did.
    """

    def jobs(self) -> tuple[Job, ...]:
        """Model by model, each scenario's repeats together."""
        combos = [
            (scenario, model, repeat)
            for model in self.models
            for scenario in self.scenarios
            for repeat in range(1, self.repeat + 1)
        ]
        return tuple(
            Job(
                scenario=scenario,
                model=model,
                repeat=repeat,
                profile=f"{self.profile}-{number}" if self.apart else self.profile,
            )
            for number, (scenario, model, repeat) in enumerate(combos, start=1)
        )

    @property
    def prompts(self) -> int:
        """Every message the run will send, if nothing is skipped."""
        turns = sum(len(scenario.turns) for scenario in self.scenarios)
        return turns * len(self.models) * self.repeat


class Observer(Protocol):
    """Somebody watching the run: the CLI prints progress, a test records it."""

    def started(self, job: Job, number: int, total: int) -> None: ...

    def turn_finished(self, job: Job, turn: TurnRecord) -> None: ...

    def turn_event(self, job: Job, index: int, line: str) -> None:
        """One line about turn ``index`` as it happens.

        Each step the harness took before sending it, the moment that step ends; then each
        step and ask of the turn itself, the moment the transcript shows it.
        """

    def finished(self, record: ScenarioRecord) -> None: ...


class Quiet:
    """An observer that says nothing."""

    def started(self, job: Job, number: int, total: int) -> None:
        """Nothing to say."""

    def turn_finished(self, job: Job, turn: TurnRecord) -> None:
        """Nothing to say."""

    def turn_event(self, job: Job, index: int, line: str) -> None:
        """Nothing to say."""

    def finished(self, record: ScenarioRecord) -> None:
        """Nothing to say."""


class Runner:
    """Holds each job's conversation and records what happened.

    ``shell`` runs the scenarios' ``host`` commands. Without one, every command is refused
    and its scenario is an ``error``: a runner nobody allowed to touch this machine cannot.
    """

    def __init__(
        self,
        hub: Hub,
        *,
        pace: Pace | None = None,
        observer: Observer | None = None,
        shell: Shell | None = None,
    ) -> None:
        self._hub = hub
        self._pace = pace or Pace()
        self._observer: Observer = observer or Quiet()
        self._shell = shell
        self._stopped: HubError | None = None

    def run(self, plan: Plan, sink: Callable[[ScenarioRecord], None]) -> HubError | None:
        """Run every job, handing each record to ``sink`` as it lands.

        Records are handed over one at a time rather than returned at the end, so whatever
        interrupts a long run -- Ctrl-C included -- leaves everything finished so far with
        the caller. Returns the fatal error that stopped the run, or ``None``.
        """
        jobs = plan.jobs()
        for number, job in enumerate(jobs, start=1):
            if self._stopped is not None:
                sink(_record(job, ERROR, reason=f"not run: the run stopped early: {self._stopped}"))
                continue
            self._observer.started(job, number, len(jobs))
            record = self.run_job(job, plan)
            sink(record)
            self._observer.finished(record)
        return self._stopped

    def run_job(self, job: Job, plan: Plan) -> ScenarioRecord:
        """One scenario with one model: its own session, from requirements to archive."""
        started = self._pace.clock()
        try:
            missing = self._unmet(job.scenario, job.profile)
            if missing:
                return _record(job, SKIPPED, reason=missing)
            session = self._hub.create_session(_session_body(job))
        except HubError as exc:
            return _record(job, ERROR, reason=self._failure(exc, "before a session existed"))
        sessions = _Sessions(
            Conversation(self._hub, session, pace=self._pace, shell=self._shell),
            start=lambda: Conversation(
                self._hub,
                self._hub.create_session(_session_body(job)),
                pace=self._pace,
                shell=self._shell,
            ),
            keep=plan.keep_sessions,
        )
        seeds: list[InvocationRecord] = []
        turns: list[TurnRecord] = []
        try:
            reason = self._seed(sessions.current, job.scenario, seeds)
            if reason:
                outcome = ERROR
            else:
                reason = self._converse(sessions, job, plan.timeout, turns)
                outcome = outcome_for(tuple(turns))
        except HubError as exc:
            reason, outcome = self._failure(exc, "mid-conversation"), ERROR
        finally:
            tidy = (*sessions.close(), *self._release(job, plan))
        reason = "; ".join(filter(None, (reason, *tidy)))
        return _record(
            job,
            outcome,
            reason=reason,
            session_id=sessions.first.session_id,
            seconds=self._pace.clock() - started,
            seed=tuple(seeds),
            turns=tuple(turns),
            usage=sessions.usage(),
        )

    def _release(self, job: Job, plan: Plan) -> tuple[str, ...]:
        """Give a conversation's own profile's sandbox back; its notes stay to be read.

        Each conversation in a profile of its own held a sandbox of its own, and the account
        may hold twenty: the eighth conversation of a two-run suite answered 503 on a full
        account, and every run before this one had left its sandbox behind as well.
        """
        if not plan.apart or plan.keep_sessions:
            return ()
        try:
            self._hub.release_workspace(job.profile)
        except HubError as exc:
            return (f"could not release the sandbox of {job.profile}: {exc}",)
        return ()

    def _unmet(self, scenario: Scenario, profile: str) -> str:
        """Asked again for every job: readiness moves, and a cold capability warms up."""
        if not scenario.requires:
            return ""
        return unmet(scenario, self._hub.capabilities(profile))

    def _seed(
        self, conversation: Conversation, scenario: Scenario, seeds: list[InvocationRecord]
    ) -> str:
        """Plant the scenario's starting state. A seed that fails makes the run an error."""
        for number, invocation in enumerate(scenario.seed, start=1):
            record = conversation.invoke(invocation)
            seeds.append(record)
            label = f"seed {number} {invocation.op}: "
            failing = invocation_failures(invocation, record, prefix=label)
            if failing:
                return failing
        return ""

    def _converse(
        self, sessions: _Sessions, job: Job, timeout: float, turns: list[TurnRecord]
    ) -> str:
        """Every turn in order. A turn left unsent, halted or never at rest ends it."""
        specs = job.scenario.turns
        for index, spec in enumerate(specs, start=1):
            limit = spec.timeout_seconds or timeout
            if spec.new_session:
                fresh = sessions.another()
                self._observer.turn_event(job, index, f"~ new session {fresh.session_id}")
            turn = sessions.current.take_turn(
                index,
                spec,
                timeout=limit,
                on_event=partial(self._observer.turn_event, job, index),
            )
            turns.append(turn)
            if turn.unsent:
                later = len(specs) - index
                either = f"; {later} later turn(s) were not sent either" if later else ""
                return f"turn {index} was not sent: {turn.unsent}{either}"
            self._observer.turn_finished(job, turn)
            if turn.halted:
                later = len(specs) - index
                unsent = f", so {later} later turn(s) were not sent" if later else ""
                return f"turn {index} was halted: {turn.halted}{unsent}"
            if turn.timed_out and index < len(specs):
                return (
                    f"turn {index} did not come to rest in {limit:.0f}s, so "
                    f"{len(specs) - index} later turn(s) were not sent"
                )
        return ""

    def _failure(self, exc: HubError, when: str) -> str:
        """Why the scenario errored; a fatal error also stops every job after it."""
        if exc.fatal:
            self._stopped = exc
        return f"stopped {when}: {exc}"


class _Sessions:
    """The sessions one scenario has held, in order: one, unless it came back another day.

    Each is closed -- cancelled and archived, unless kept -- when the next one starts, the way
    a person's last conversation is over before their next begins. What could not be tidied
    is kept to be reported with the scenario, never raised over the reason it ended.
    """

    def __init__(
        self, first: Conversation, *, start: Callable[[], Conversation], keep: bool
    ) -> None:
        self.first = first
        self.current = first
        self._start = start
        self._keep = keep
        self._held = [first]
        self._tidy: list[str] = []

    def another(self) -> Conversation:
        """Close this session and start the next on the same profile, as a person would."""
        self._tidy.extend(self.current.close(keep=self._keep))
        self.current = self._start()
        self._held.append(self.current)
        return self.current

    def close(self) -> tuple[str, ...]:
        return (*self._tidy, *self.current.close(keep=self._keep))

    def usage(self) -> dict[str, object]:
        """Every session's usage added up; a count in only one of them is still counted."""
        total: dict[str, object] = {}
        for conversation in self._held:
            for key, value in conversation.usage.items():
                earlier = total.get(key, 0)
                counted = isinstance(value, int) and not isinstance(value, bool)
                if counted and isinstance(earlier, int) and not isinstance(earlier, bool):
                    total[key] = earlier + value
                else:
                    total[key] = value
        return total


def unmet(scenario: Scenario, capabilities: list[dict[str, Any]]) -> str:
    """Why ``scenario`` cannot start, given ``GET /v1/capabilities``; empty when it can.

    Readiness is the hub's own ``usable`` flag, for the profile the run uses. A capability
    that is not listed at all is not installed on this hub, which is a skip too: the
    scenario is about something this deployment does not have.
    """
    listed = {str(row.get("id")): row for row in capabilities}
    missing: list[str] = []
    for capability in scenario.requires:
        row = listed.get(capability)
        if row is None:
            missing.append(f"{capability} is not installed on this hub")
        elif row.get("usable") is not True:
            state = row.get("state") or "not ready"
            detail = f": {row['detail']}" if row.get("detail") else ""
            missing.append(f"{capability} is {state}{detail}")
    return "; ".join(missing)


def _session_body(job: Job) -> dict[str, object]:
    scenario = job.scenario
    return {
        "title": TITLE.format(name=scenario.name),
        "model": job.model,
        "profile": job.profile,
        "permission_mode": scenario.permission_mode,
        "incognito": scenario.incognito,
        "input_policy": INPUT_POLICY,
    }


def _record(  # noqa: PLR0913 - every field is a column of the report
    job: Job,
    outcome: str,
    *,
    reason: str = "",
    session_id: str = "",
    seconds: float = 0.0,
    seed: tuple[InvocationRecord, ...] = (),
    turns: tuple[TurnRecord, ...] = (),
    usage: dict[str, object] | None = None,
) -> ScenarioRecord:
    scenario = job.scenario
    return ScenarioRecord(
        scenario=scenario.qualified,
        suite=scenario.suite,
        name=scenario.name,
        summary=scenario.summary,
        path=scenario.path,
        digest=scenario.digest,
        model=job.model,
        repeat=job.repeat,
        outcome=outcome,
        reason=reason,
        session_id=session_id,
        seconds=round(seconds, 3),
        seed=seed,
        turns=turns,
        usage=dict(usage or {}),
    )


__all__ = [
    "DEFAULT_PROFILE",
    "DEFAULT_TIMEOUT",
    "Job",
    "Observer",
    "Plan",
    "Quiet",
    "Runner",
    "unmet",
]
