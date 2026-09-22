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

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import number_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.work import State as WorkState
from lucy_api.work import StillRunningError, UnknownWorkError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.packs.context import PackContext
    from lucy_api.work import Record, Registry

WORK_MARKDOWN = """# Work in flight

Anything that outlives the step that started it lands here: a helper you asked to research
something, a download, a long command. They are one list because the question is one
question.

`work.list` is what is running now. `work.check` is what finished since you last looked --
it names the result's size, never the result. `work.result` reads one. `work.wait` blocks
for a bounded time and does not stop the work when it gives up. `work.cancel` stops
something, and is safe to call twice.

Prefer finishing your answer and saying what is still running over waiting. "The download is
going, I'll tell you when it lands" is a complete reply.
"""

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


def _line(record: Record) -> dict[str, Any]:
    """One entry, as the model reads it. Never the payload."""
    return {
        "id": record.id,
        "kind": record.kind.value,
        "role": record.role,
        "objective": record.objective,
        "state": record.state.value,
        "progress": record.progress or record.detail,
    }


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
        return WORK_MARKDOWN

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="work.stop",
                title="Stop something that is running",
                description="Cancel a helper, a download or a command before it finishes.",
                risk="write",
                covers=("work.cancel",),
            ),
        )

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

        async def run_list(_run: RunContext[Any]) -> dict[str, Any]:
            return _list(registry, session_id)

        async def run_check(_run: RunContext[Any]) -> dict[str, Any]:
            return _check(registry, session_id)

        async def run_result(run: RunContext[Any]) -> dict[str, Any]:
            return _result(registry, str(run.input["work_id"]))

        async def run_wait(run: RunContext[Any]) -> dict[str, Any]:
            asked = run.input.get("seconds")
            return await _wait(
                registry,
                str(run.input["work_id"]),
                float(asked) if asked is not None else DEFAULT_WAIT_SECONDS,
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
    return {
        "running": [_line(record) for record in running],
        "count": len(running),
        "advice": (
            "Nothing here is waiting on you. Carry on; a notice arrives when one finishes."
            if running
            else "Nothing is running."
        ),
    }


def _check(registry: Registry, session_id: str) -> dict[str, Any]:
    notices = registry.drain(session_id)
    return {
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
        result = await registry.wait(work_id, min(seconds, MAX_WAIT_SECONDS))
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
    "MAX_WAIT_SECONDS",
    "WORK_MARKDOWN",
    "WorkPack",
]
