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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from lucy_api.context.framing import Origin, frame_result
from lucy_api.context.scrub import scrub, scrub_tree
from lucy_api.context.types import Trust
from lucy_api.model.types import ModelRefusedError, ModelUnavailableError, Reply, Request, Stop
from lucy_api.turn.claims import UNBACKED, unbacked
from lucy_api.turn.repetition import Repetition
from lucy_api.turn.stop import Budget, Spent, Termination, Verdict, should_stop, warning_for
from lucy_api.turn.window import RESULT_TOKEN_CAP, attach_needles, needle_from, without_needles
from lucy_api.turn.window import window as result_window

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from lucy_api.decide.uses import Recovery
    from lucy_api.model.types import Chunk, Message, Provider, Usage

MAX_PLAN_REPAIRS = 2
"""How many times a malformed plan is handed back for correction.

Two, because the first repair usually works and the third never does. A model that cannot
answer the schema after two tries has misunderstood the task, not the format, and spending
the rest of the turn on it helps nobody."""

EMPTY_REPLY = (
    "Your previous reply was empty: nothing reached the person, and no plan was sent. "
    "Answer the person in prose, or send a plan."
)
"""What the model is told after a round in which it said nothing at all.

An empty reply used to end the turn as a success. The person saw nothing, the turn said
`completed`, and on the weakest model it happened on ordinary requests -- "Show me what's in
the calculator folder" came back as a finished turn with no words and no steps. So it is
handed back like a malformed plan, from the same repair budget, and a model that still says
nothing after that fails the turn rather than succeeding at silence.
"""

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
    unavailable: bool = False
    """The provider could not be reached. The one failure a second model can answer."""
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
    plan_schema: dict[str, Any] | Callable[[], dict[str, Any]] | None = None
    """What the model may plan against. A callable is asked again every round, so a
    capability the person turned off mid-turn is not offered at the next one."""
    append: Append | None = None
    budget: Budget | None = None
    model: str = ""
    cancelled: Callable[[], bool | Awaitable[bool]] | None = None
    clock: Callable[[], float] = time.monotonic
    on_chunk: Callable[[Chunk], Awaitable[None]] | None = None
    recovery: Recovery | None = None
    opening_notice: str = ""
    opening_plan: dict[str, Any] | None = None
    """Steps to run before the model is asked anything: the calls a person just approved.

    Run through the same executor, recorded the same way, so the model's first round reads
    their results like any other tool result.
    """
    """A sentence for the first round only, decided before the turn starts.

    This exists because a resumed turn looks, from the transcript, exactly like a turn where
    the work was already done. A parked plan is held in memory and dropped, the model's own
    proposed plan is never written down when the reply was plan-only, and what survives is
    the request and `{"approved": true}` in the person's voice. Something has to say that the
    approved call still has not run, and the notice channel is where this turn already puts
    facts about itself.
    """

    max_output_tokens: int = 4096
    temperature: float | None = None
    thinking: str = "default"
    result_token_cap: int = RESULT_TOKEN_CAP
    fallback_provider: Provider | None = None
    fallback_model: str = ""
    max_thinking_tokens: int = 0


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
    opening: str = ""
    """Spent on the first round and then empty. A fact about how this turn started, which
    stops being true the moment the model has read it."""
    claim_checked: bool = False
    """Whether a reply claiming undone work was already held back once this turn."""


async def run_turn(turn: Turn) -> Outcome:
    """Run one turn to completion, or to the first reason it has to stop."""
    cycle = _Cycle(
        turn=turn,
        outcome=Outcome(),
        limits=turn.budget if turn.budget is not None else Budget(),
        is_cancelled=turn.cancelled if turn.cancelled is not None else _never,
        started=turn.clock(),
        repetition=Repetition(),
        opening=turn.opening_notice,
    )

    if turn.opening_plan is not None and turn.execute is not None:
        opening = Reply(plan=turn.opening_plan, stop=Stop.tool_use)
        opened = await _run_plan(
            cycle, opening, Round(text="", plan=turn.opening_plan), turn.execute
        )
        if opened is not None:
            return opened

    while True:
        ended = await _early_stop(
            cycle.outcome, cycle.limits, cycle.outcome.spent, cycle.is_cancelled
        )
        if ended is not None:
            return ended

        notice = "\n".join(
            filter(
                None,
                (
                    cycle.opening,
                    warning_for(cycle.limits, cycle.outcome.spent),
                    cycle.repair_notice,
                ),
            )
        )
        cycle.opening = ""
        system, messages = await turn.assemble(notice)
        request = Request(
            messages=messages,
            system=system,
            plan_schema=turn.plan_schema() if callable(turn.plan_schema) else turn.plan_schema,
            model=turn.model,
            max_output_tokens=turn.max_output_tokens,
            temperature=turn.temperature,
            thinking=turn.thinking,
            max_thinking_tokens=turn.max_thinking_tokens,
        )

        reply = await _ask(turn, request, cycle.outcome)
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
    if reply.plan is None and not cycle.claim_checked and unbacked(reply.text, outcome.rounds):
        # Held back before it reaches the transcript: a false "I've saved that" read once is
        # believed. The round was paid for, so it is kept, but not what it said.
        cycle.claim_checked = True
        outcome.rounds.append(replace(round_, text=""))
        cycle.repair_notice = UNBACKED
        return None
    await _assistant_item(turn, reply.text)
    ended = await _early_stop(outcome, cycle.limits, outcome.spent, cycle.is_cancelled)
    if ended is not None:
        outcome.rounds.append(round_)
        return ended
    executor = turn.execute
    if reply.plan is None or executor is None:
        if _said_nothing(reply):
            return await _nothing_said(cycle, round_)
        return _finished(outcome, round_, reply)
    return await _run_plan(cycle, reply, round_, executor)


def _said_nothing(reply: Reply) -> bool:
    """No words and no plan. A refusal is a fact about the request, not an empty reply."""
    return reply.plan is None and not reply.text.strip() and reply.stop is not Stop.refusal


async def _nothing_said(cycle: _Cycle, round_: Round) -> Outcome | None:
    """Hand an empty reply back once or twice; after that, fail rather than succeed silently."""
    outcome = cycle.outcome
    # Kept for its accounting -- the round was paid for -- but whitespace is not something
    # the model said, and it must not reach the answer the turn reports.
    outcome.rounds.append(replace(round_, text=""))
    if cycle.turn.append is not None:
        await cycle.turn.append("error", "tool", {"code": "empty_reply", "detail": EMPTY_REPLY})
    if cycle.repairs < MAX_PLAN_REPAIRS:
        cycle.repairs += 1
        cycle.repair_notice = EMPTY_REPLY
        return None
    outcome.termination = Termination.failed
    outcome.detail = "the model replied with nothing, each time it was asked"
    return outcome


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
    if turn.recovery is not None:
        cycle.repair_notice = await turn.recovery.observe(
            [f"{step.operation}: {step.error}" for step in executed if step.status == "error"]
        )
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


FALLBACK_NOTE = "(Answered by {model} because the chosen model was unavailable.)"


async def _ask(turn: Turn, request: Request, outcome: Outcome) -> Reply | None:
    """One model call, with one retry on the fallback model when the chosen one is out.

    Every failure is recorded on the turn rather than raised, because a turn is a durable
    unit: an exception thrown from here becomes a 500 with no transcript behind it, and the
    person is left with a conversation that simply stopped.

    The fallback is tried once and only for unavailability. A refusal, a malformed call or
    an empty stream is a fact about the request, and the second model would only repeat it.
    The person is told which voice answered.
    """
    reply = await _call(turn.provider, request, outcome, turn.on_chunk)
    if reply is not None or outcome.termination is not Termination.failed:
        return reply
    if turn.fallback_provider is None or not outcome.unavailable:
        return None
    outcome.termination = Termination.success
    outcome.detail = ""
    outcome.unavailable = False
    # Emptied rather than set: the fallback provider was built for its own model and
    # falls back to it. Carrying the first model's id across would ask the second
    # provider for a model only the first has.
    fallback_request = replace(request, model="")
    reply = await _call(turn.fallback_provider, fallback_request, outcome, turn.on_chunk)
    if reply is None:
        return None
    return _mark_fallback(reply, turn.fallback_model)


UNAVAILABLE = "the model was unavailable"
"""How unavailability reads to a person. `Outcome.unavailable` is what the fallback
reads: rewording this sentence must never be able to disable retrying."""


async def _call(
    provider: Provider,
    request: Request,
    outcome: Outcome,
    on_chunk: Callable[[Chunk], Awaitable[None]] | None,
) -> Reply | None:
    """One call to one provider. Returns the reply, or `None` having recorded why not.

    When a callback is attached, the provider's stream is used so a subscriber can see
    reasoning as it arrives. Spoken text is still assembled from the `done` chunk: a plan
    round's deltas are JSON, and forwarding those to a person is a leak of the wire format.

    For an exception we do not recognise, only the type name is kept. A third-party client
    often puts the provider's response body in the message, and a response body has no
    business in a transcript or a log line.
    """
    try:
        reply = await _complete(provider, request, on_chunk)
    except ModelRefusedError as exc:
        outcome.termination = Termination.refused
        outcome.detail = str(exc)
        outcome.stop_reason = Stop.refusal
        return None
    except ModelUnavailableError as exc:
        outcome.termination = Termination.failed
        outcome.detail = f"{UNAVAILABLE}: {exc}"
        outcome.unavailable = True
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


def _mark_fallback(reply: Reply, model: str) -> Reply:
    named = model or reply.model
    note = FALLBACK_NOTE.format(model=named)
    if reply.text.startswith(note):
        return reply
    text = f"{note}\n\n{reply.text}" if reply.text else note
    return replace(reply, text=text)


async def _complete(
    provider: Provider,
    request: Request,
    on_chunk: Callable[[Chunk], Awaitable[None]] | None,
) -> Reply | None:
    if on_chunk is None:
        return await provider.complete(request)
    reply: Reply | None = None
    async for chunk in provider.stream(request):
        await on_chunk(chunk)
        if chunk.reply is not None:
            reply = chunk.reply
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
    cleaned = scrub(body) if isinstance(body, str) else scrub_tree(body)
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
    "EMPTY_REPLY",
    "MAX_PLAN_REPAIRS",
    "RESULT_TOKEN_CAP",
    "Outcome",
    "Round",
    "Step",
    "Turn",
    "run_turn",
]
