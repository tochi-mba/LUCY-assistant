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
session they are for (see :mod:`lucy_api.evals.direct`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.evals.checks import check_invocation
from lucy_api.evals.conversation import Conversation, Pace
from lucy_api.evals.hub import HubError
from lucy_api.evals.results import ERROR, SKIPPED, ScenarioRecord, outcome_for

if TYPE_CHECKING:
    from collections.abc import Callable

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


@dataclass(frozen=True, slots=True)
class Plan:
    """What a run will do, decided in full before anything is created."""

    scenarios: tuple[Scenario, ...]
    models: tuple[str, ...]
    repeat: int = 1
    profile: str = DEFAULT_PROFILE
    timeout: float = DEFAULT_TIMEOUT
    keep_sessions: bool = False

    def jobs(self) -> tuple[Job, ...]:
        """Model by model, each scenario's repeats together."""
        return tuple(
            Job(scenario=scenario, model=model, repeat=repeat)
            for model in self.models
            for scenario in self.scenarios
            for repeat in range(1, self.repeat + 1)
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

    def finished(self, record: ScenarioRecord) -> None: ...


class Quiet:
    """An observer that says nothing."""

    def started(self, job: Job, number: int, total: int) -> None:
        """Nothing to say."""

    def turn_finished(self, job: Job, turn: TurnRecord) -> None:
        """Nothing to say."""

    def finished(self, record: ScenarioRecord) -> None:
        """Nothing to say."""


class Runner:
    """Holds each job's conversation and records what happened."""

    def __init__(
        self, hub: Hub, *, pace: Pace | None = None, observer: Observer | None = None
    ) -> None:
        self._hub = hub
        self._pace = pace or Pace()
        self._observer: Observer = observer or Quiet()
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
            missing = self._unmet(job.scenario, plan.profile)
            if missing:
                return _record(job, SKIPPED, reason=missing)
            session = self._hub.create_session(_session_body(job, plan))
        except HubError as exc:
            return _record(job, ERROR, reason=self._failure(exc, "before a session existed"))
        conversation = Conversation(self._hub, session, pace=self._pace)
        seeds: list[InvocationRecord] = []
        turns: list[TurnRecord] = []
        try:
            reason = self._seed(conversation, job.scenario, seeds)
            if reason:
                outcome = ERROR
            else:
                reason = self._converse(conversation, job, plan.timeout, turns)
                outcome = outcome_for(tuple(turns))
        except HubError as exc:
            reason, outcome = self._failure(exc, "mid-conversation"), ERROR
        finally:
            tidy = conversation.close(keep=plan.keep_sessions)
        reason = "; ".join(filter(None, (reason, *tidy)))
        return _record(
            job,
            outcome,
            reason=reason,
            session_id=conversation.session_id,
            seconds=self._pace.clock() - started,
            seed=tuple(seeds),
            turns=tuple(turns),
            usage=conversation.usage,
        )

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
            failing = [
                check
                for check in check_invocation(invocation, record, prefix=label)
                if not check.passed
            ]
            if failing:
                return "; ".join(f"{check.name} ({check.detail})" for check in failing)
        return ""

    def _converse(
        self, conversation: Conversation, job: Job, timeout: float, turns: list[TurnRecord]
    ) -> str:
        """Every turn in order. A turn that never came to rest ends the conversation."""
        specs = job.scenario.turns
        for index, spec in enumerate(specs, start=1):
            limit = spec.timeout_seconds or timeout
            turn = conversation.take_turn(index, spec, timeout=limit)
            turns.append(turn)
            self._observer.turn_finished(job, turn)
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


def _session_body(job: Job, plan: Plan) -> dict[str, object]:
    scenario = job.scenario
    return {
        "title": TITLE.format(name=scenario.name),
        "model": job.model,
        "profile": plan.profile,
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
