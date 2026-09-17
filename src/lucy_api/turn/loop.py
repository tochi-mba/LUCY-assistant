"""The one loop.

Append, think, run, append, repeat. That is the whole of it, and keeping it that way is a
decision rather than an accident: the systems that work at this are reported, again and
again, not to have been using a framework. Everything else in this codebase is harness
around these forty lines.

    while not done:
        context = assemble(session)          # what the model sees, priced
        reply   = await model.complete(...)  # text, or a plan
        if no plan: finish
        results = await runtime.execute(...) # the steps, with refs between them
        append(results)                      # as items, framed and scrubbed

## Why the model answers with a plan instead of a tool call

A tool call at a time means every value that moves between two calls is rendered into the
context so the model can copy it into the next one. Searching and then saving the top three
results costs the entire search result in tokens, twice. A plan says "run these, and step
two takes step one's output by reference", and the data never becomes tokens at all.

## What the loop does that a naive loop does not

**It tells the model where it stands before it runs out.** A budget warning arrives at
eighty percent, not at a hundred, because a model with one round left will use it to write
down where it got to and a model that discovers the limit by hitting it writes nothing.

**It notices repetition and says something.** Three identical calls is a model that has
lost track, not a model being thorough. It gets told what it already tried and what came
back, which is usually enough.

**It never lets a tool result in unframed.** Everything a tool returns crosses a trust
boundary: it is scrubbed, then framed as a reported claim with its origin. A result that
says "ignore your previous instructions" arrives as a quoted thing somebody's server said.

**A dropped connection is not a cancellation.** The loop runs detached from whoever asked
for it. Closing a laptop must not kill an hour of work, and cancelling is an explicit,
idempotent act rather than a side effect of a socket closing.

**A malformed plan is a correction, not a crash.** weftai returns text describing what was
wrong with the plan, and that text goes back to the model, which fixes it. Capped, because
a model that cannot produce a valid plan twice will not produce one on the tenth attempt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from lucy_api.context.framing import Origin, frame_result
from lucy_api.context.scrub import scrub
from lucy_api.context.types import Trust
from lucy_api.model.types import ModelRefusedError, ModelUnavailableError, Request, Stop
from lucy_api.turn.repetition import Repetition
from lucy_api.turn.stop import Budget, Spent, Termination, Verdict, should_stop, warning_for
from lucy_api.turn.window import RESULT_TOKEN_CAP, attach_needles, needle_from, without_needles
from lucy_api.turn.window import window as result_window

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from lucy_api.model.types import Message, Provider, Reply, Usage

MAX_PLAN_REPAIRS = 2
"""How many times a malformed plan is handed back for correction.

Two, because the first repair usually works and the third never does. A model that cannot
answer the schema after two tries has misunderstood the task, not the format, and spending
the rest of the turn on it helps nobody."""

# RESULT_TOKEN_CAP lives on the window so spill, focus and the loop share one number.


@dataclass(frozen=True, slots=True)
class Step:
    """One executed step, as the loop records it."""

    id: str
    operation: str
    status: str
    note: str = ""
    summary: str = ""
    notices: tuple[str, ...] = ()
    error: str = ""
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class Round:
    """One pass: what the model said, and what running it produced."""

    text: str
    reasoning: str = ""
    plan: dict[str, Any] | None = None
    steps: tuple[Step, ...] = ()
    usage: Usage | None = None
    repaired: int = 0


@dataclass(slots=True)
class Outcome:
    """How the turn ended, and what it cost."""

    termination: Termination = Termination.success
    detail: str = ""
    stop_reason: Stop = Stop.end_turn
    rounds: list[Round] = field(default_factory=list)
    spent: Spent = field(default_factory=Spent)

    @property
    def text(self) -> str:
        """Everything the model said, in order. What a person reads."""
        return "\n\n".join(round_.text for round_ in self.rounds if round_.text)


type ExecutePlan = Callable[[dict[str, Any]], Awaitable[Any]]
"""Runs a plan and returns a weftai ExecutionResult. Injected so the loop can be driven
without a registry, which is what makes a golden transcript possible."""

type Append = Callable[[str, str, object], Awaitable[None]]
"""How an item reaches the transcript: kind, role, content."""

type Assemble = Callable[[str], Awaitable[tuple[str, Sequence[Message]]]]
"""Builds the prompt, given a notice to put in front of the model. See `context.build`."""


@dataclass(frozen=True, slots=True)
class Turn:
    """Everything one turn needs, named rather than spread across nine parameters.

    Every collaborator is a callable rather than an object with a class, and that is what
    makes a golden transcript possible: a test supplies a scripted model, a scripted
    executor and a list it appends to, and asserts the exact items, the exact prompt and
    the exact accounting with no network anywhere.
    """

    provider: Provider
    assemble: Assemble
    execute: ExecutePlan | None = None
    plan_schema: dict[str, Any] | None = None
    append: Append | None = None
    budget: Budget | None = None
    model: str = ""
    cancelled: Callable[[], bool] | None = None
    clock: Callable[[], float] = time.monotonic


async def run_turn(turn: Turn) -> Outcome:
    """Run one turn to completion, or to the first reason it has to stop."""
    limits = turn.budget if turn.budget is not None else Budget()
    outcome = Outcome()
    repetition = Repetition()
    started = turn.clock()
    repairs = 0
    repair_notice = ""
    is_cancelled = turn.cancelled if turn.cancelled is not None else _never

    while True:
        spent = outcome.spent
        if is_cancelled():
            outcome.termination = Termination.cancelled
            outcome.detail = "stopped on request"
            outcome.stop_reason = Stop.cancelled
            return outcome

        verdict = should_stop(limits, spent)
        if verdict.stop:
            return _stopped(outcome, verdict)

        notice = "\n".join(filter(None, (warning_for(limits, spent), repair_notice)))
        system, messages = await turn.assemble(notice)
        request = Request(
            messages=messages, system=system, plan_schema=turn.plan_schema, model=turn.model
        )

        reply = await _ask(turn.provider, request, outcome)
        if reply is None:
            return outcome

        outcome.spent = _add(spent, reply, turn.clock() - started)
        round_ = Round(
            text=reply.text, reasoning=reply.reasoning, plan=reply.plan, usage=reply.usage
        )

        if turn.append is not None and reply.text:
            await turn.append("message", "assistant", reply.text)

        if reply.plan is None or turn.execute is None:
            return _finished(outcome, round_, reply)

        result = await turn.execute(without_needles(reply.plan))
        attach_needles(reply.plan, result)
        executed: tuple[Step, ...]
        if not _is_valid(result):
            repair_notice = "The previous plan was invalid: " + _issue_text(result)
            await _invalid_item(turn, repair_notice)
            if repairs < MAX_PLAN_REPAIRS:
                repairs += 1
                outcome.rounds.append(round_)
                continue
            executed = (
                Step(
                    id="plan",
                    operation="(plan)",
                    status="error",
                    error=_issue_text(result) or "the plan could not be understood",
                ),
            )
        else:
            executed = _record(result, repetition=repetition)
            repair_notice = ""
        if turn.append is not None:
            for step in executed:
                await turn.append("tool_result", "tool", _item_for(step))
        outcome.rounds.append(
            Round(
                text=reply.text,
                reasoning=reply.reasoning,
                plan=reply.plan,
                steps=executed,
                usage=reply.usage,
                repaired=repairs,
            )
        )
        if repair_notice:
            outcome.termination = Termination.failed
            outcome.detail = "the plan could not be repaired"
            return outcome
        repairs = 0
        outcome.spent = Spent(
            iterations=outcome.spent.iterations,
            tokens=outcome.spent.tokens,
            seconds=turn.clock() - started,
            tool_calls=outcome.spent.tool_calls + len(executed),
        )


def _finished(outcome: Outcome, round_: Round, reply: Reply) -> Outcome:
    outcome.rounds.append(round_)
    outcome.stop_reason = reply.stop
    if reply.stop is Stop.refusal:
        outcome.termination = Termination.refused
        outcome.detail = "the model declined"
    return outcome


async def _invalid_item(turn: Turn, notice: str) -> None:
    if turn.append is not None:
        await turn.append("error", "tool", {"code": "invalid_plan", "detail": notice})


async def _ask(provider: Provider, request: Request, outcome: Outcome) -> Reply | None:
    """One model call. Returns the reply, or `None` having recorded why there was not one.

    Every failure is recorded on the turn rather than raised, because a turn is a durable
    unit: an exception thrown from here becomes a 500 with no transcript behind it, and the
    person is left with a conversation that simply stopped.

    For an exception we do not recognise, only the type name is kept. A third-party client
    often puts the provider's response body in the message, and a response body has no
    business in a transcript or a log line.
    """
    try:
        return await provider.complete(request)
    except ModelRefusedError as exc:
        outcome.termination = Termination.refused
        outcome.detail = str(exc)
        outcome.stop_reason = Stop.refusal
    except ModelUnavailableError as exc:
        outcome.termination = Termination.failed
        outcome.detail = f"the model was unavailable: {exc}"
    except Exception as exc:
        outcome.termination = Termination.failed
        outcome.detail = f"the model call failed ({type(exc).__name__})"
    return None


def _is_valid(result: Any) -> bool:
    return not result.get("issues")


def _issue_text(result: Any) -> str:
    text = result.get("text") or ""
    return str(text)


def _record(result: Any, *, repetition: Repetition) -> tuple[Step, ...]:
    """Turn executed steps into items, scrubbed and framed on the way in."""
    steps: list[Step] = []
    for raw in result.get("steps", ()):
        operation = str(raw.get("operation", ""))
        summary, extra = _summarise(raw)
        notices = tuple(raw.get("notices") or ()) + extra
        repetition.record(operation, raw.get("input"), summary or raw.get("error") or "no result")
        steps.append(
            Step(
                id=str(raw.get("id", "")),
                operation=operation,
                status=str(raw.get("status", "ok")),
                note=str(raw.get("note", "")),
                summary=summary,
                notices=notices,
                error=str(raw.get("error") or ""),
                duration_ms=float(raw.get("durationMs", 0.0)),
            )
        )
    return tuple(steps)


def _item_for(step: Step) -> dict[str, Any]:
    """One executed step as a transcript item.

    The note is carried because it is the sentence a person reads in the log, in an
    approval prompt and in a compaction summary. Losing it here would mean the transcript
    records what was run and not what it was for.
    """
    return {
        "step_id": step.id,
        "operation": step.operation,
        "status": step.status,
        "note": step.note,
        "summary": step.summary,
        "notices": list(step.notices),
        "error": step.error,
        "duration_ms": step.duration_ms,
    }


def _summarise(raw: Any) -> tuple[str, tuple[str, ...]]:
    """What a step contributes to the context: scrubbed, framed, and capped.

    Every tool result has crossed a trust boundary -- it came from a service, a page, or
    somebody else's server -- so it is neutralised and then rendered as a reported claim
    with its origin attached, never as something the model was told to do.

    Overflow spills rather than drops. The model may also re-run a step with
    ``show_from`` set to a unique snippet of the spilled view; display starts there,
    and the same head-and-tail spill applies if that window is still too large.
    """
    body = raw.get("data")
    if body is None:
        return "", ()
    text = body if isinstance(body, str) else repr(body)
    cleaned = scrub(text)
    operation = str(raw.get("operation", ""))
    origin = Origin(capability=operation.split(".", 1)[0] or "a tool")
    viewed = result_window(cleaned.text, needle_from(raw))
    framed = frame_result(viewed.text, origin, trust=Trust.untrusted)
    return framed, viewed.notices


def _add(spent: Spent, reply: Reply, seconds: float) -> Spent:
    usage = reply.usage
    return Spent(
        iterations=spent.iterations + 1,
        tokens=spent.tokens + usage.input_tokens + usage.output_tokens,
        seconds=seconds,
        tool_calls=spent.tool_calls,
    )


def _stopped(outcome: Outcome, verdict: Verdict) -> Outcome:
    outcome.termination = verdict.termination
    outcome.detail = verdict.detail
    return outcome


def _never() -> bool:
    return False


__all__ = [
    "MAX_PLAN_REPAIRS",
    "RESULT_TOKEN_CAP",
    "Outcome",
    "Round",
    "Step",
    "Turn",
    "run_turn",
]
