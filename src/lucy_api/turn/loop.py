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

import inspect
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

    from lucy_api.model.types import Chunk, Message, Provider, Reply, Usage

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
    permission: str = ""
    operation: str = ""
    description: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    asks: tuple[dict[str, Any], ...] = ()

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
    cancelled: Callable[[], bool | Awaitable[bool]] | None = None
    clock: Callable[[], float] = time.monotonic
    on_chunk: Callable[[Chunk], Awaitable[None]] | None = None
    max_output_tokens: int = 4096
    temperature: float | None = None
    thinking: str = "default"
    result_token_cap: int = RESULT_TOKEN_CAP


@dataclass(slots=True)
class _Cycle:
    """Mutable loop state. Keeps ``run_turn`` a loop rather than a parameter list."""

    turn: Turn
    outcome: Outcome
    limits: Budget
    is_cancelled: Callable[[], bool | Awaitable[bool]]
    started: float
    repetition: Repetition
    repairs: int = 0
    repair_notice: str = ""


async def run_turn(turn: Turn) -> Outcome:
    """Run one turn to completion, or to the first reason it has to stop."""
    cycle = _Cycle(
        turn=turn,
        outcome=Outcome(),
        limits=turn.budget if turn.budget is not None else Budget(),
        is_cancelled=turn.cancelled if turn.cancelled is not None else _never,
        started=turn.clock(),
        repetition=Repetition(),
    )

    while True:
        ended = await _early_stop(
            cycle.outcome, cycle.limits, cycle.outcome.spent, cycle.is_cancelled
        )
        if ended is not None:
            return ended

        notice = "\n".join(
            filter(None, (warning_for(cycle.limits, cycle.outcome.spent), cycle.repair_notice))
        )
        system, messages = await turn.assemble(notice)
        request = Request(
            messages=messages,
            system=system,
            plan_schema=turn.plan_schema,
            model=turn.model,
            max_output_tokens=turn.max_output_tokens,
            temperature=turn.temperature,
            thinking=turn.thinking,
        )

        reply = await _ask(turn.provider, request, cycle.outcome, turn.on_chunk)
        if reply is None:
            return cycle.outcome

        finished = await _after_reply(cycle, reply)
        if finished is not None:
            return finished


async def _after_reply(cycle: _Cycle, reply: Reply) -> Outcome | None:
    """Consume one model reply: cancel, speak, or run the plan.

    A speaking reply is recorded before the cancel check, because the person asked to stop
    a turn that had already produced words, not to un-say them.
    """
    turn = cycle.turn
    outcome = cycle.outcome
    spent = outcome.spent
    outcome.spent = _add(spent, reply, turn.clock() - cycle.started)
    round_ = Round(text=reply.text, reasoning=reply.reasoning, plan=reply.plan, usage=reply.usage)
    await _assistant_item(turn, reply.text)
    ended = await _early_stop(outcome, cycle.limits, outcome.spent, cycle.is_cancelled)
    if ended is not None:
        outcome.rounds.append(round_)
        return ended
    executor = turn.execute
    if reply.plan is None or executor is None:
        return _finished(outcome, round_, reply)
    return await _run_plan(cycle, reply, round_, executor)


async def _run_plan(
    cycle: _Cycle, reply: Reply, round_: Round, executor: ExecutePlan
) -> Outcome | None:
    """Execute the plan on a reply that already survived the cancel check."""
    turn = cycle.turn
    outcome = cycle.outcome
    result = await executor(without_needles(reply.plan))
    attach_needles(reply.plan, result)
    waiting = _permission_issues(result)
    if waiting:
        return _parked(outcome, round_, reply.plan, waiting)
    denied_notice = await _record_denial(turn, outcome, round_, result)
    if denied_notice:
        cycle.repair_notice = denied_notice
        return None
    executed: tuple[Step, ...]
    repair_notice = ""
    if not _is_valid(result):
        repair_notice = "The previous plan was invalid: " + _issue_text(result)
        await _invalid_item(turn, repair_notice)
        if cycle.repairs < MAX_PLAN_REPAIRS:
            cycle.repairs += 1
            cycle.repair_notice = repair_notice
            outcome.rounds.append(round_)
            return None
        executed = (
            Step(
                id="plan",
                operation="(plan)",
                status="error",
                error=_issue_text(result) or "the plan could not be understood",
            ),
        )
    else:
        executed = _record(result, repetition=cycle.repetition, cap=turn.result_token_cap)
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
            repaired=cycle.repairs,
        )
    )
    if repair_notice:
        outcome.termination = Termination.failed
        outcome.detail = "the plan could not be repaired"
        return outcome
    cycle.repairs = 0
    cycle.repair_notice = ""
    outcome.spent = Spent(
        iterations=outcome.spent.iterations,
        tokens=outcome.spent.tokens,
        seconds=turn.clock() - cycle.started,
        tool_calls=outcome.spent.tool_calls + len(executed),
    )
    return None


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


async def _assistant_item(turn: Turn, text: str) -> None:
    if turn.append is not None and text:
        await turn.append("message", "assistant", text)


async def _record_denial(turn: Turn, outcome: Outcome, round_: Round, result: Any) -> str:
    """Record a person's refusal and return guidance for the model's next round."""
    denied = _denied_steps(result, round_.plan)
    if not denied:
        return ""
    if turn.append is not None:
        for step in denied:
            await turn.append("tool_result", "tool", _item_for(step))
    outcome.rounds.append(
        Round(
            text=round_.text,
            reasoning=round_.reasoning,
            plan=round_.plan,
            steps=denied,
            usage=round_.usage,
        )
    )
    # A refusal is a tool result for the next model round, not a malformed plan to
    # retry unchanged. Include the concrete denial so the model can explain it.
    detail = _issue_text(result) or "The requested action was denied."
    return f"{detail} Choose a safe alternative."


async def _ask(
    provider: Provider,
    request: Request,
    outcome: Outcome,
    on_chunk: Callable[[Chunk], Awaitable[None]] | None = None,
) -> Reply | None:
    """One model call. Returns the reply, or `None` having recorded why there was not one.

    Every failure is recorded on the turn rather than raised, because a turn is a durable
    unit: an exception thrown from here becomes a 500 with no transcript behind it, and the
    person is left with a conversation that simply stopped.

    When a callback is attached, the provider's stream is used so a subscriber can see
    reasoning as it arrives. Spoken text is still assembled from the `done` chunk: a plan
    round's deltas are JSON, and forwarding those to a person is a leak of the wire format.

    For an exception we do not recognise, only the type name is kept. A third-party client
    often puts the provider's response body in the message, and a response body has no
    business in a transcript or a log line.
    """
    reply: Reply | None = None
    try:
        if on_chunk is None:
            return await provider.complete(request)
        async for chunk in provider.stream(request):
            await on_chunk(chunk)
            if chunk.reply is not None:
                reply = chunk.reply
    except ModelRefusedError as exc:
        outcome.termination = Termination.refused
        outcome.detail = str(exc)
        outcome.stop_reason = Stop.refusal
        return None
    except ModelUnavailableError as exc:
        outcome.termination = Termination.failed
        outcome.detail = f"the model was unavailable: {exc}"
        return None
    except Exception as exc:
        outcome.termination = Termination.failed
        outcome.detail = f"the model call failed ({type(exc).__name__})"
        return None
    if reply is None:
        outcome.termination = Termination.failed
        outcome.detail = "the model stream ended without a reply"
        return None
    return reply


def _parked(
    outcome: Outcome,
    round_: Round,
    plan: dict[str, Any] | None,
    waiting: tuple[dict[str, Any], ...],
) -> Outcome:
    """A write that needs a person is a parked turn, not a broken plan."""
    first = waiting[0] if waiting else {}
    step = _first_step(plan)
    outcome.termination = Termination.input_required
    outcome.detail = str(first.get("message") or "")
    outcome.permission = str(first.get("permission") or "")
    outcome.operation = str(first.get("operation") or step.get("op") or "")
    outcome.description = str(first.get("description") or step.get("note") or outcome.detail)
    raw_input = first.get("arguments")
    if not isinstance(raw_input, dict):
        raw_input = step.get("input")
    outcome.arguments = raw_input if isinstance(raw_input, dict) else {}
    outcome.asks = waiting
    outcome.rounds.append(round_)
    return outcome


def _is_valid(result: Any) -> bool:
    return not result.get("issues")


def _permission_issues(result: Any) -> tuple[dict[str, Any], ...]:
    """Writes that need a person are not a malformed plan, and must not be 'repaired'."""
    issues = result.get("issues") or ()
    return tuple(
        item
        for item in issues
        if isinstance(item, dict) and item.get("code") == "permission_required"
    )


def _denied_steps(result: Any, plan: dict[str, Any] | None) -> tuple[Step, ...]:
    """Render explicit denials as tool results the model can route around."""
    issues = result.get("issues") or ()
    planned = _steps_by_operation(plan)
    denied: list[Step] = []
    for item in issues:
        if not isinstance(item, dict) or item.get("code") != "permission_denied":
            continue
        operation = str(item.get("operation") or "")
        step = planned.get(operation, {})
        denied.append(
            Step(
                id=str(step.get("id") or item.get("permission") or "permission"),
                operation=operation,
                status="denied",
                note=str(step.get("note") or item.get("description") or ""),
                error=str(item.get("message") or "the person denied this action"),
            )
        )
    return tuple(denied)


def _steps_by_operation(plan: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(plan, dict):
        return {}
    rows = plan.get("steps")
    if not isinstance(rows, list):
        return {}
    return {
        str(item.get("op") or item.get("operation") or ""): item
        for item in rows
        if isinstance(item, dict)
    }


def _first_step(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {}
    steps = plan.get("steps")
    if isinstance(steps, list) and steps and isinstance(steps[0], dict):
        return steps[0]
    return {}


def _issue_text(result: Any) -> str:
    text = result.get("text") or ""
    return str(text)


def _record(
    result: Any, *, repetition: Repetition, cap: int = RESULT_TOKEN_CAP
) -> tuple[Step, ...]:
    """Turn executed steps into items, scrubbed and framed on the way in."""
    steps: list[Step] = []
    for raw in result.get("steps", ()):
        operation = str(raw.get("operation", ""))
        summary, extra = _summarise(raw, cap=cap)
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


def _summarise(raw: Any, *, cap: int = RESULT_TOKEN_CAP) -> tuple[str, tuple[str, ...]]:
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
    viewed = result_window(cleaned.text, needle_from(raw), cap=cap)
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


async def _early_stop(
    outcome: Outcome,
    limits: Budget,
    spent: Spent,
    is_cancelled: Callable[[], bool | Awaitable[bool]],
) -> Outcome | None:
    flagged = is_cancelled()
    if inspect.isawaitable(flagged):
        flagged = await flagged
    if flagged:
        outcome.termination = Termination.cancelled
        outcome.detail = "stopped on request"
        outcome.stop_reason = Stop.cancelled
        return outcome
    verdict = should_stop(limits, spent)
    if verdict.stop:
        return _stopped(outcome, verdict)
    return None


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
