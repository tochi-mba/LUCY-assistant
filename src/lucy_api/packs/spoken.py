"""A failed step, said in product words with the next thing to do.

`clients.errors` says its messages are for a log or a pack, never for a prompt, and that a pack
translates them. Only repos did. Every other pack let `memory answered 409: ...` through, so the
model read a service name and a status code -- which the house rules keep out of the prompt --
and no next step. Each operation's `run` is wrapped here, once, when the turn's registry is
built: whatever a handler did not catch itself is said as what it means for the person.

The exception types are untouched. Probes and the routes read them; only what a step's error
says to the model changes, and the original stays chained for the log.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING, Any

from weftai.operation import Operation

from lucy_api.clients.errors import (
    AbsentError,
    ConflictError,
    DownstreamError,
    ForbiddenError,
    NotConnectedError,
    PreconditionError,
    RateLimitedError,
    ReauthenticationError,
    RejectedError,
)
from lucy_api.packs.http import DownstreamError as TransportError

if TYPE_CHECKING:
    from weftai.operation import AnyOperation, RunContext


class StepRefusedError(RuntimeError):
    """A sibling's no, in words the model can act on."""


TIMED_OUT = re.compile(r"^Step '(?P<id>[^']+)' timed out after (?P<ms>\d+)ms\b.*$", re.DOTALL)

RAN_OUT = (
    "Step '{id}' ran out of time after {seconds}s and was stopped. Retry once if it was a "
    "read; if it keeps happening, tell the person it is slow right now."
)
"""weftai's own timeout advice was "raise the step timeout": something the model cannot do,
and the only way it could try is a settings write the settings page forbids."""


def spoken(operation: AnyOperation) -> AnyOperation:
    """The same operation, whose uncaught sibling refusals are said in product words."""
    capability = operation.name.split(".", 1)[0]
    original = operation.run

    async def run(context: RunContext[Any]) -> Any:
        try:
            answer = original(context)
            return await answer if inspect.isawaitable(answer) else answer
        except DownstreamError as exc:
            raise StepRefusedError(said(exc, capability)) from exc
        except TransportError as exc:
            raise StepRefusedError(UNREACHABLE.format(capability=capability)) from exc

    return Operation(
        name=operation.name,
        description=operation.description,
        input=operation.input,
        output=operation.output,
        effects=operation.effects,
        sources=operation.sources,
        examples=operation.examples,
        present=operation.present,
        run=run,
    )


UNREACHABLE = (
    "{capability} could not be reached just now. Retry once; if it fails again, tell the person."
)


WITH_DETAIL: tuple[tuple[type[DownstreamError], str, str], ...] = (
    (
        AbsentError,
        "Nothing has that id",
        "Use an id from an earlier result or from your prompt; do not guess one.",
    ),
    (
        ConflictError,
        "That is at its limit",
        "That is a limit or a pinned value, not a fault: tell the person.",
    ),
    (PreconditionError, "It changed", "It changed since you read it: read it again, then retry."),
    (
        RejectedError,
        "That value was not accepted",
        "Change what you sent; do not retry it as it is.",
    ),
)
"""Refusals whose detail is worth reading -- which id, which limit, what would be accepted --
each with what it falls back to when the service said nothing, and the next step."""


def said(exc: DownstreamError, capability: str) -> str:
    """What a refusal means for the person, and what to do about it."""
    if isinstance(exc, NotConnectedError):
        return f"{capability} is not connected for this person: offer them the connect link."
    if isinstance(exc, RateLimitedError):
        wait = f" Try again in {exc.retry_after:.0f}s." if exc.retry_after else " Try again later."
        return f"{capability} is limiting requests.{wait}"
    if isinstance(exc, (ForbiddenError, ReauthenticationError)):
        return f"{capability} does not allow this for this person right now: tell them."
    for kind, fallback, next_step in WITH_DETAIL:
        if isinstance(exc, kind):
            return f"{exc.detail.rstrip('. ') or fallback}. {next_step}"
    return UNREACHABLE.format(capability=capability)


def timed_out(error: str) -> str:
    """weftai's timeout, said with a next step the model can take; any other error as it is."""
    match = TIMED_OUT.match(error)
    if match is None:
        return error
    seconds = int(match["ms"]) // 1000
    return RAN_OUT.format(id=match["id"], seconds=seconds)


__all__ = ["RAN_OUT", "UNREACHABLE", "StepRefusedError", "said", "spoken", "timed_out"]
