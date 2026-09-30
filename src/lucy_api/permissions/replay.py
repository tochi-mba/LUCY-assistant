"""Run an approved call with the steps it reads from, the way the plan that asked for it would.

A plan is checked whole before any step runs, so a plan whose write needs a person parks
before its reads have run. The person approves one call, and the hub runs it for them. It
used to run that call alone. A call that read another step's result could not: on
2026-09-30 a plan found a song with ``music.find`` and played it with
``music.play {"uri": "$find_track"}``, the play parked, was approved, and ran on its own with
the literal text ``$find_track`` for a URI. The music service refused it, the model planned
the same two steps again, and the turn looped through ten approvals, each failing the same
way.

So an approval records what its call needs from the plan that asked for it: the call's own
step and every step it reads from, directly or through another step, in plan order. On
resume those steps run together, under their own ids, and a reference resolves exactly as
it would have. A step it needs that was itself parked must have been approved too: a call
that reads from something the person refused is not run, and the model is told why.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from weftai.refs import parse_ref

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from lucy_api.permissions.approvals import ApprovedCall

LEGACY_ID = "approved_{index}"
"""The step id an approval recorded before this module existed runs under."""


@dataclass(frozen=True, slots=True)
class Needs:
    """What one parked call needs from its plan to run as it was planned."""

    step: str
    """The call's own step id in the plan."""
    steps: tuple[dict[str, Any], ...]
    """The call's step and every step it reads from, in plan order: ``id``, ``op``, ``input``."""
    gated: tuple[str, ...]
    """Steps among ``steps``, other than the call's own, that were parked beside it."""
    plan: str
    """A digest of the plan it came from, so calls from one plan are run as one."""


@dataclass(frozen=True, slots=True)
class Replay:
    """The plan to run before the model is asked anything, and the calls left out of it."""

    plan: dict[str, Any] | None
    ran: tuple[ApprovedCall, ...]
    held: tuple[tuple[ApprovedCall, str], ...] = ()
    """Approved calls that cannot run, each with the sentence that says why."""


def references(value: object) -> tuple[str, ...]:
    """The step ids a step input reads from, in the order they appear, each once."""
    found: list[str] = []
    for text in _strings(value):
        parsed = parse_ref(text)
        if parsed["ok"] and parsed["ref"]["id"] not in found:
            found.append(parsed["ref"]["id"])
    return tuple(found)


def needs(plan: Mapping[str, Any] | None, step_id: str, *, parked: Iterable[str] = ()) -> Needs:
    """What ``step_id`` needs from ``plan``: itself and every step it reads from.

    A reference to a step the plan does not have is left for the executor to refuse, with
    its own message, rather than guessed at here.
    """
    steps = _steps(plan)
    by_id = {str(step.get("id") or ""): step for step in steps}
    wanted: set[str] = set()
    pending = [step_id]
    while pending:
        current = pending.pop()
        if current in wanted or current not in by_id:
            continue
        wanted.add(current)
        pending.extend(references(by_id[current].get("input")))
    ordered = tuple(_plain_step(step) for step in steps if str(step.get("id") or "") in wanted)
    others = {str(step["id"]) for step in ordered} - {step_id}
    return Needs(
        step=step_id,
        steps=ordered,
        gated=tuple(sorted(others & set(parked))),
        plan=digest(plan),
    )


def digest(plan: Mapping[str, Any] | None) -> str:
    """The same plan, however its keys were ordered, has the same digest."""
    canonical = json.dumps(plan or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def replay(calls: Sequence[ApprovedCall]) -> Replay:
    """One plan holding every approved call and the steps each reads from."""
    approved = {(call.plan, call.step) for call in calls if call.step}
    ran: list[ApprovedCall] = []
    held: list[tuple[ApprovedCall, str]] = []
    steps: dict[str, dict[str, Any]] = {}
    for index, call in enumerate(calls, 1):
        if not call.step:
            steps[LEGACY_ID.format(index=index)] = {
                "id": LEGACY_ID.format(index=index),
                "op": call.operation,
                "input": call.arguments,
            }
            ran.append(call)
            continue
        refused = [step for step in call.gated if (call.plan, step) not in approved]
        if refused:
            held.append((call, _refused(call, refused)))
            continue
        clash = [
            step["id"] for step in call.needs if step["id"] in steps and steps[step["id"]] != step
        ]
        if clash:
            held.append((call, _clashed(call, clash)))
            continue
        for step in call.needs:
            steps.setdefault(step["id"], step)
        ran.append(call)
    plan = {"steps": list(steps.values())} if steps else None
    return Replay(plan=plan, ran=tuple(ran), held=tuple(held))


def _refused(call: ApprovedCall, refused: Sequence[str]) -> str:
    named = ", ".join(refused)
    return (
        f"{call.operation} was approved but did not run: it reads from {named}, "
        "which the person did not approve."
    )


def _clashed(call: ApprovedCall, clash: Sequence[str]) -> str:
    named = ", ".join(clash)
    return (
        f"{call.operation} was approved but did not run: it came from a different plan, "
        f"whose step {named} is not the one that ran."
    )


def _steps(plan: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    raw = plan.get("steps") if plan else None
    return [step for step in raw if isinstance(step, dict)] if isinstance(raw, list) else []


def _plain_step(step: Mapping[str, Any]) -> dict[str, Any]:
    """The three keys the executor reads. ``note`` is refused by its schema, so it goes."""
    raw = step.get("input")
    return {
        "id": str(step.get("id") or ""),
        "op": str(step.get("op") or ""),
        "input": raw if isinstance(raw, dict) else {},
    }


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


__all__ = ["Needs", "Replay", "digest", "needs", "references", "replay"]
