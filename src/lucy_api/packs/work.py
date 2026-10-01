"""Checking in on what is still running, whoever started it.

This is the capability that makes "I'll tell you when it lands" an honest sentence. A
helper, a download and a shell command all appear here, in one list, because from where the
model is sitting they are one question.

Four things it deliberately does not do:

It does not **start** anything. Starting is the job of whichever capability the work belongs
to -- a download starts in the capability that downloads. Putting a generic "start something"
operation here would give the model a way to run work that no capability claimed, and there
would be nowhere to look up what it was allowed to do.

It does not **push results**. `work.check` says a thing finished and roughly how big the
answer is; reading it is `work.result`, a separate act. A job that produced forty megabytes
of log does not arrive uninvited in a context.

It does not **poll**. `work.wait` exists and has a deadline, and the description says to
prefer the notice. A model that loops on `work.check` has been given the wrong shape.

And it does not **cancel implicitly**. A person closing a tab is not a cancellation; only
`work.cancel` is, and calling it twice is calling it once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import number_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.context.types import Trust
from lucy_api.core.errors import LucyError
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.prompt.docs import capability_doc
from lucy_api.work import State as WorkState
from lucy_api.work import StillRunningError, UnknownWorkError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.packs.context import PackContext
    from lucy_api.work import Record, Registry
    from lucy_api.work.subscriptions import SubscriptionSeam

DEFAULT_WAIT_SECONDS = 30.0
"""How long `work.wait` waits when the model does not say.

Short, because the default should be the one that is usually wrong in the cheap direction:
coming back and saying "still going" costs a sentence, and holding a turn open for two
minutes costs the person the whole conversation.
"""

MAX_WAIT_SECONDS = 120.0
"""The longest `work.wait` will hold a turn open.

Two minutes, because a turn that waits longer than that is a turn a person has stopped
watching. Anything slower should be answered with "it is still going" and picked up next
turn, which is what the notice is for.
"""


NO_CHECKINS = (
    "Check-ins are not available in this turn, so nothing can bring the conversation back "
    "later on its own. Say what should happen next, and when."
)
ONE_TIME = "Give exactly one of `at` (a date and time with its offset) or `in_seconds`."
NEEDS_OFFSET = (
    "`at` needs a date and time with its offset, as 2026-10-01T19:24:00+01:00 or ...Z. The "
    "live block says what time it is now, and in which zone."
)
IN_THE_PAST = "That time has passed; the live block says what time it is now."


def _line(record: Record, now: datetime) -> dict[str, Any]:
    """One entry, as the model reads it. Never the payload."""
    line: dict[str, Any] = {
        "id": record.id,
        "kind": record.kind.value,
        "role": record.role,
        "objective": record.objective,
        "state": record.state.value,
        "progress": record.progress or record.detail,
    }
    due = record.tags.get("due")
    if due is not None:
        line["due_at"] = _iso(float(due))
        line["due_in_seconds"] = max(0, round(float(due) - now.timestamp()))
    return line


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=UTC).isoformat(timespec="seconds")


PRODUCED = frozenset({"work.result"})
"""Operations that hand back what a piece of work produced -- a helper's report, a command's
output -- which is downstream of whatever that work read."""


class WorkPack:
    """Everything in flight for this session, as one capability.

    It has no downstream and no credential, so it is `ready` whenever the turn has a
    registry at all. That matters: this is the capability a model reaches for *because*
    something else is slow or broken, and a capability that disappears when the system is
    busy is the one you needed.
    """

    id = "work"
    title = "Work in flight"
    summary = "See what is still running, read a finished result, or stop something."

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="work.stop",
                title="Stop something that is running",
                description="Cancel a helper, a download or a command before it finishes.",
                risk="write",
                covers=("work.cancel",),
            ),
            Permission(
                id="work.checkin",
                title="Come back later on its own",
                description=(
                    "Open a turn at a time Lucy chose, to look at something again or finish "
                    "what you asked, under the standing consent you give when you say yes."
                ),
                risk="write",
                covers=("work.checkin",),
            ),
        )

    def result_trust(self, operation: str, data: object) -> Trust:
        """Lucy's own bookkeeping, except what a finished piece of work produced."""
        del data
        return Trust.untrusted if operation in PRODUCED else Trust.observed

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:
        if context.work is None:
            return Availability(state=State.not_configured, detail="no work registry this turn")
        running = len(context.work.running(context.session_id))
        detail = f"{running} running" if running else "nothing running"
        return Availability(state=State.ready, detail=detail)

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        registry, session_id = context.work, context.session_id
        if registry is None:
            return ()
        seam = context.subscriptions

        async def run_list(_run: RunContext[Any]) -> dict[str, Any]:
            return _list(registry, session_id)

        async def run_checkin(run: RunContext[Any]) -> dict[str, Any]:
            return await _checkin(registry, seam, run.input)

        async def run_check(_run: RunContext[Any]) -> dict[str, Any]:
            return _check(registry, session_id)

        async def run_result(run: RunContext[Any]) -> dict[str, Any]:
            return _result(registry, str(run.input["work_id"]))

        async def run_wait(run: RunContext[Any]) -> dict[str, Any]:
            asked = run.input.get("seconds")
            wanted = float(asked) if asked is not None else DEFAULT_WAIT_SECONDS
            return await _wait(
                registry,
                str(run.input["work_id"]),
                run.ctx.within_step(min(wanted, MAX_WAIT_SECONDS)),
            )

        async def run_cancel(run: RunContext[Any]) -> dict[str, Any]:
            return _cancel(registry, str(run.input["work_id"]))

        return (
            define_operation(
                {
                    "name": "work.list",
                    "description": (
                        "What is still running for this session -- helpers, downloads and "
                        "commands together, with what each one is for and how long it has "
                        "been going (running, in flight, status, progress, background)."
                    ),
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": run_list,
                }
            ),
            define_operation(
                {
                    "name": "work.check",
                    "description": (
                        "What finished since you last checked. Says how it went and roughly "
                        "how big the answer is; never the answer itself. Read one with "
                        "work.result (finished, done, notices, completed)."
                    ),
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": run_check,
                }
            ),
            define_operation(
                {
                    "name": "work.result",
                    "description": (
                        "Read what one finished piece of work produced. Fetching is "
                        "deliberate: a large result stays out of the conversation until you "
                        "ask for it (result, output, findings, report)."
                    ),
                    "input": object_schema(
                        {
                            "work_id": string_schema().describe(
                                "The id handed back when the work started."
                            )
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": run_result,
                }
            ),
            define_operation(
                {
                    "name": "work.wait",
                    "description": (
                        "Wait for one piece of work, for a bounded time. Giving up does not "
                        "stop it. Prefer answering now and picking the result up when its "
                        "notice arrives (wait, block, until, finish)."
                    ),
                    "input": object_schema(
                        {
                            "work_id": string_schema().describe("The id to wait for."),
                            "seconds": number_schema()
                            .optional()
                            .describe(f"How long to wait, at most {MAX_WAIT_SECONDS:.0f}."),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": run_wait,
                }
            ),
            define_operation(
                {
                    "name": "work.checkin",
                    "description": (
                        "Come back to this conversation at a time, on your own, to do what "
                        "`objective` says: look at a pull request again, see whether a job "
                        "finished, carry on once a shop has opened. The conversation is woken "
                        "then with a notice naming the objective, and the turn it opens can "
                        "act. Say `at` with its offset, or `in_seconds`; at least a minute "
                        "away, at most a week. Prefer a watch or a subscription when something "
                        "can tell you the moment it happens; a check-in is for a time (later, "
                        "remind, at, backstop, follow up)."
                    ),
                    "input": object_schema(
                        {
                            "objective": string_schema().describe(
                                "One plain sentence saying what to do then, written for the "
                                "person: it is what the woken turn reads first."
                            ),
                            "at": string_schema()
                            .optional()
                            .describe("When, as 2026-10-01T19:24:00+01:00 or ...Z."),
                            "in_seconds": number_schema()
                            .optional()
                            .describe("Or: how many seconds from now."),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": run_checkin,
                }
            ),
            define_operation(
                {
                    "name": "work.cancel",
                    "description": (
                        "Stop something that is running. Safe to call twice; the second call "
                        "changes nothing (cancel, stop, abort, kill)."
                    ),
                    "input": object_schema(
                        {"work_id": string_schema().describe("The id to stop.")}
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": run_cancel,
                }
            ),
        )


# --------------------------------------------------------------------------------------
# Handlers. Each is handed the registry it was bound with rather than reaching for it
# through the run context, which is what makes "no registry, no operations" an invariant
# with no branch to test: a handler that exists is one whose registry existed.
# --------------------------------------------------------------------------------------


def _list(registry: Registry, session_id: str) -> dict[str, Any]:
    running = registry.running(session_id)
    now = registry.now()
    return {
        "running": [_line(record, now) for record in running],
        "count": len(running),
        "advice": (
            "Nothing here is waiting on you. Carry on; a notice arrives when one finishes."
            if running
            else "Nothing is running."
        ),
    }


async def _checkin(registry: Registry, seam: SubscriptionSeam | None, given: Any) -> dict[str, Any]:
    """Open a check-in from what the model gave, or say exactly why not.

    The time is checked against the registry's clock before anything is recorded: a refusal
    names the bound it crossed, and the live block already says what time it is.
    """
    if seam is None:
        return _refusal("unavailable", NO_CHECKINS)
    objective = " ".join(str(given.get("objective") or "").split())
    if not objective:
        return _refusal("invalid", "Say in `objective` what to do when the check-in fires.")
    now = registry.now().timestamp()
    due_at = _due_time(now, given.get("at"), given.get("in_seconds"))
    if isinstance(due_at, str):
        return _refusal("invalid", due_at)
    try:
        opened = await seam.checkin(objective=objective, due_at=due_at, delay_seconds=due_at - now)
    except LucyError as exc:
        return _refusal("invalid", str(exc))
    return {
        "id": opened.handle.id,
        "due_at": _iso(due_at),
        "in_seconds": round(due_at - now),
        "advice": (
            "Finish your answer and say when you will look again. The conversation is woken "
            "then; work.cancel with this id calls it off."
        ),
    }


def _due_time(now: float, at: object, in_seconds: object) -> float | str:
    """The moment asked for, as a timestamp, or the sentence saying why there is none."""
    if (at is None) == (in_seconds is None):
        return ONE_TIME
    if at is not None:
        parsed = _moment(str(at))
        if parsed is None:
            return NEEDS_OFFSET
        due_at = parsed.timestamp()
    else:
        due_at = now + float(in_seconds)  # type: ignore[arg-type]
    return IN_THE_PAST if due_at <= now else due_at


def _moment(text: str) -> datetime | None:
    """An ISO 8601 moment with an offset, or ``None`` for anything else."""
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _check(registry: Registry, session_id: str) -> dict[str, Any]:
    notices = registry.drain(session_id)
    teams = registry.drain_teams(session_id)
    checked: dict[str, Any] = {
        "finished": [
            {
                "id": notice.id,
                "kind": notice.kind.value,
                "role": notice.role,
                "objective": notice.objective,
                "state": notice.state.value,
                "seconds": round(notice.elapsed_seconds, 1),
                "result_tokens": notice.tokens,
                "detail": notice.detail,
                **({"group": notice.group} if notice.group else {}),
            }
            for notice in notices
        ],
        "count": len(notices),
        "advice": (
            "These are notices, not results. Read one with work.result when you need it."
            if notices
            else "Nothing has finished since you last checked."
        ),
    }
    if teams:
        # A group whose last member has ended: every member is done, whichever way.
        checked["groups"] = [
            {"group": team.group, "ids": list(team.ids), "line": team.line()} for team in teams
        ]
    return checked


def _result(registry: Registry, work_id: str) -> dict[str, Any]:
    try:
        result = registry.result(work_id)
    except UnknownWorkError as exc:
        return _refusal("unknown", str(exc))
    except StillRunningError as exc:
        return _refusal("still_running", str(exc))
    return {
        "id": result.id,
        "state": result.state.value,
        "payload": result.payload,
        "detail": result.detail,
    }


async def _wait(registry: Registry, work_id: str, seconds: float) -> dict[str, Any]:
    try:
        result = await registry.wait(work_id, seconds)
    except UnknownWorkError as exc:
        return _refusal("unknown", str(exc))
    except StillRunningError as exc:
        return _refusal("still_running", str(exc))
    return {
        "id": result.id,
        "state": result.state.value,
        "result_tokens": result.tokens,
        "detail": result.detail,
        "advice": "It finished. Read it with work.result.",
    }


def _cancel(registry: Registry, work_id: str) -> dict[str, Any]:
    try:
        record = registry.cancel(work_id)
    except UnknownWorkError as exc:
        return _refusal("unknown", str(exc))
    stopped = record.state is WorkState.running
    return {
        "id": record.id,
        "state": record.state.value,
        "advice": (
            "Asked it to stop. Its notice will say so."
            if stopped
            else f"It had already {record.state.value}; nothing was stopped."
        ),
    }


def _refusal(reason: str, message: str) -> dict[str, Any]:
    """A refusal the model can act on, rather than an exception it can only apologise for.

    The sentence is the whole point. "It is still running; wait for its notice" tells a model
    what to do next; a stack trace tells it that something went wrong and leaves it guessing
    between retrying, giving up and asking the person.
    """
    return {"status": reason, "message": message}


__all__ = [
    "DEFAULT_WAIT_SECONDS",
    "IN_THE_PAST",
    "MAX_WAIT_SECONDS",
    "NEEDS_OFFSET",
    "NO_CHECKINS",
    "ONE_TIME",
    "WorkPack",
]
