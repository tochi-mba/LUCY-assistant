"""Turning capabilities into the tools one turn may use.

This is where the plan's central bet is cashed in. The model does not get a list of
functions to call one at a time; it gets a **registry**, and it answers with a *plan* — a
handful of steps where a later step references an earlier one's result by name. The data
those steps produce never passes through the model's context on its way between them.

The difference is not a micro-optimisation. Searching the web and then writing the three
best results to a file is, in a call-at-a-time world, a search result rendered into the
context so the model can copy three URLs out of it and hand them to the next call. Here it
is two steps and a `$hits[0,1,2]`, and the page text is never a token the person paid for.

## Probing, and what happens when a probe fails

Every pack is probed concurrently at the top of a turn. A probe that fails does not fail the
turn: that capability reports itself unavailable, its operations are absent for this turn,
and the model is told in the state block. One service being down must never be the reason a
person cannot ask a question that had nothing to do with it.

Availability is cached per (account, profile, pack) for a few seconds so a conversation
that never mentions music does not wait on music's devices list every turn. Operations
still run locally on a hit, because they close over this turn's context. A connect,
disconnect, settings write, or 502 naming a missing credential drops the row.

## Why an unusable capability is *absent* rather than present-and-failing

A tool the model can call and that always errors is worse than no tool. It will call it,
apologise, and often try again. Absent means it cannot be called, cannot be half-called, and
the model's own account of what it can do stays true.

(The MCP surface does the opposite and keeps the tool listed. That is not an inconsistency:
a client that cached a tool list has no way back from a tool that vanished, and a model that
cannot see a tool cannot explain what connecting it would let the person do. Different
audience, different answer. See `packs/base.py`.)

## Deferred loading

Tool-selection accuracy degrades once a model is choosing among more than thirty or forty
operations, and a hub fronting the family is comfortably in that range. So past a
threshold the registry binds only the most recently used capabilities plus `help`, and the
model reaches the rest through `capabilities.use`. One line in the prompt names the
categories, so it knows what is there to ask for.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, cast

from weftai import create_formatter, create_registry, create_runtime, standard_operations

from lucy_api.packs.base import Availability, Bound, Catalogue, State
from lucy_api.packs.collections import ALL as COLLECTIONS
from lucy_api.settings.policy import ALWAYS_ON, TurnPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from weftai.operation import AnyOperation
    from weftai.registry import Registry
    from weftai.results.types import ResultStore
    from weftai.schema.types import CollectionType

    from lucy_api.packs.base import CapabilityPack
    from lucy_api.packs.context import PackContext

DEFER_ABOVE = 6
"""How many ready capabilities before the registry starts holding some back.

Counted in capabilities rather than operations because that is the unit a person and a model
both reason about, and because a capability's operations are useless apart from each other.
"""

KEEP_RECENT = 4
"""How many of them stay bound when deferral kicks in, most recently used first."""

ALWAYS = ("help", "work", "agents")
"""Never deferred. Without `help` the model cannot ask for what was deferred, which would
make deferral a one-way door. `work` and `agents` are the check-in path for everything
that outlives a step, so hiding them when the system is busy hides the one capability
that exists specifically for that case."""

PROBE_SECONDS = 5.0
"""How long a capability has to say whether it is usable.

Short on purpose: this runs before every turn, and a person waiting on a reply should not
be paying for a service that has stopped answering. Not answering in time is the same
answer as being down."""

SLOW_SERVICES = frozenset({"research", "mcp", "music"})
"""Built-in capabilities whose steps and probes are allowed longer.

A page fetch taking twelve seconds is not a bug, and failing it at ten only produces a
retry that takes twelve too. Music is here because a play answers only once the player has
been seen playing, which can take fifteen seconds on a device waking up; at ten the step
gave up on a command that was working, and a model told it had failed sends it again,
restarting the track. Extensions own any additional timeout policy they need.
"""

SLOW_MULTIPLE = 3
"""What "allowed longer" is worth, for both a step and the probe in front of it.

One figure, used in both places, because they answer the same question about the same
service. Two figures drift, and the way they drift is the whole bug below: a capability
generous enough to fetch a page but not to say that it can.
"""


def _ceiling(pack_id: str, seconds: float) -> float:
    """How long this capability has to answer, before it is called down."""
    return seconds * SLOW_MULTIPLE if pack_id in SLOW_SERVICES else seconds


async def probe_all(
    packs: Sequence[CapabilityPack], context: PackContext, *, seconds: float = PROBE_SECONDS
) -> Catalogue:
    """Probe every capability at once, tolerating any of them failing.

    A probe is a network call to somebody else's service, so it gets a timeout. A capability
    that does not answer in time is unavailable for this turn rather than a turn that does
    not happen.

    The slow ones get the same allowance their steps get. They are slow because of what they
    do -- research asks a browser and a model provider whether they are there -- and the
    first such call after an idle spell measured 5.06 seconds against a ceiling of 5.00, in
    front of a service that answered every later call in 47 milliseconds. So the capability
    documented as needing longer was the one capability reported down, on the first turn of
    every session, about a service that was working.
    """

    async def one(pack: CapabilityPack) -> Bound:
        cached = None
        if context.probes is not None:
            cached = context.probes.get(context.account_id, context.profile, pack.id)
        if cached is not None:
            availability = cached
        else:
            try:
                async with asyncio.timeout(_ceiling(pack.id, seconds)):
                    availability = await pack.probe(context)
            except TimeoutError:
                availability = Availability(
                    state=State.unavailable,
                    detail="did not answer in time",
                    checked_at=time.time(),
                )
            except Exception as exc:
                availability = Availability(
                    state=State.unavailable,
                    detail=f"unreachable ({type(exc).__name__})",
                    checked_at=time.time(),
                )
            if context.probes is not None:
                context.probes.put(context.account_id, context.profile, pack.id, availability)
        operations = tuple(pack.operations(context)) if availability.usable else ()
        return Bound(pack=pack, availability=availability, operations=operations)

    catalogue = Catalogue(bound=tuple(await asyncio.gather(*(one(pack) for pack in packs))))
    return apply_disabled(catalogue, context.policy.all_disabled)


def choose_bound(
    catalogue: Catalogue,
    *,
    recent: Sequence[str] = (),
    defer_above: int = DEFER_ABOVE,
    keep_recent: int = KEEP_RECENT,
) -> tuple[tuple[Bound, ...], tuple[str, ...]]:
    """Which capabilities are bound this turn, and which are held back.

    Returns both, because the second is not a detail: the model is told what it is not
    holding, by name, so that `capabilities.use` is a thing it knows to reach for rather
    than something it has to guess exists.
    """
    ready = catalogue.ready()
    if len(ready) <= defer_above:
        return ready, ()

    order = {pack_id: index for index, pack_id in enumerate(recent)}
    ranked = sorted(ready, key=lambda item: (order.get(item.pack.id, len(order)), item.pack.id))
    kept: list[Bound] = []
    deferred: list[str] = []
    spent = 0
    for item in ranked:
        if item.pack.id in ALWAYS:
            # Free, not first in the queue. `ALWAYS` is documented as "never deferred" and
            # `KEEP_RECENT` as "how many of them stay bound, most recently used first" -- but
            # appending these to `kept` charged them against that budget, so three always-on
            # capabilities ate three of the four slots. On a stock family that deferred
            # `workspace` and `watch` on every fresh session, which cost a `capabilities.use`
            # round before any file could be touched, and told the model its workspace was
            # ready and unusable in the same prompt.
            kept.append(item)
        elif spent < keep_recent:
            kept.append(item)
            spent += 1
        else:
            deferred.append(item.pack.id)
    # Back into catalogue order: a registry whose operation order changes between turns
    # ends the prompt cache for no reason at all.
    keep_ids = {item.pack.id for item in kept}
    return tuple(item for item in ready if item.pack.id in keep_ids), tuple(sorted(deferred))


def apply_disabled(catalogue: Catalogue, disabled: Sequence[str]) -> Catalogue:
    """Hide capabilities the person turned off. Help, work and helpers stay.

    A disabled pack is absent from the registry, which is how the model learns it must not
    offer it. The live-state listing still names it so the model can say it is off rather
    than inventing a connect link.
    """
    blocked = {name for name in disabled if name not in ALWAYS_ON}
    if not blocked:
        return catalogue
    bound: list[Bound] = []
    for item in catalogue.bound:
        if item.pack.id not in blocked:
            bound.append(item)
            continue
        bound.append(
            Bound(
                pack=item.pack,
                availability=Availability(
                    state=State.disabled,
                    detail="turned off in settings",
                    checked_at=item.availability.checked_at,
                ),
                operations=(),
            )
        )
    return Catalogue(bound=tuple(bound))


def build_registry(
    operations: Sequence[AnyOperation], *, with_standard: bool = True
) -> Registry[Any]:
    """One registry for one turn, plus the operations every collection gets for free.

    Cheap enough to build per turn, which is what makes capability gating per-person
    possible at all.

    `filter`, `count`, `countBy`, `distinct`, `mostCommon`, `first`, `pick` and `details`
    are generated for each declared collection. They are worth having for one reason above
    the others: they run against a result that is **already stored**, so "how many of those
    were confirmed" is arithmetic over something already paid for rather than a second call
    and a second page of tokens. Without them a model answers that question by asking for
    the whole list again and counting in its head, which is both slower and less reliable.
    """
    generated: list[AnyOperation] = []
    if with_standard:
        for declared in _collections_in_play(operations):
            generated.extend(standard_operations(declared))
    return create_registry({"operations": [*operations, *generated]})


def _collections_in_play(
    operations: Sequence[AnyOperation],
) -> tuple[CollectionType[Any, Any], ...]:
    """Only the collections something bound this turn can actually produce.

    Generating the free operations for every declared collection would add dozens of tools
    a model cannot use, and tool-selection accuracy falls away sharply once it is choosing
    among too many. `note.countBy` is worth having when notes are in play and is pure noise
    when they are not -- and noise in a tool list is not free, it is paid for on every turn
    in both tokens and wrong choices.
    """
    produced = {
        getattr(operation.output, "name", "")
        for operation in operations
        if getattr(operation.output, "kind", "") == "collection"
    }
    return tuple(declared for declared in COLLECTIONS if declared.name in produced)


def limits_for(bound: Sequence[Bound], policy: TurnPolicy | None = None) -> dict[str, Any]:
    """Step and plan timeouts, widened when a slow capability is in play.

    Keys are camelCase because weftai's option TypedDicts are `total=False`: a snake_case
    key is not an error, it is silently dropped, and the limit you thought you set is the
    default. That failure is invisible until something times out early in production.
    """
    limits = policy if policy is not None else TurnPolicy()
    slow = any(item.pack.id in SLOW_SERVICES for item in bound)
    multiple = SLOW_MULTIPLE if slow else 1
    return {
        "maxSteps": limits.max_steps,
        "maxParallel": limits.max_parallel,
        "stepTimeoutMs": limits.step_timeout_ms * multiple,
        "planTimeoutMs": limits.plan_timeout_ms * multiple,
    }


def build_runtime(
    registry: Registry[Any],
    store: ResultStore | None = None,
    *,
    limits: dict[str, Any] | None = None,
    policy: TurnPolicy | None = None,
) -> Any:
    """The runtime that executes a plan.

    `failure` stays `continue`. One dead service should fail its own step, skip whatever
    depended on it, and let the model read a sentence about it — not abandon four unrelated
    steps that had already succeeded. `abort` is for the rare plan where partial execution
    is worse than none, and that is a per-plan decision rather than a default.
    """
    budgets = policy if policy is not None else TurnPolicy()
    options: dict[str, Any] = {
        "registry": registry,
        "failure": "continue",
        # Named rather than left to the library's defaults. The numbers are the person's
        # settings, clamped onto the turn's policy; a formatter that is not given budgets
        # is a formatter nobody has thought about.
        "formatter": create_formatter(
            {
                "budgets": {
                    "read": budgets.render_read_tokens,
                    "preview": budgets.render_preview_tokens,
                    "total": budgets.render_total_tokens,
                }
            }
        ),
    }
    if store is not None:
        options["store"] = store
    options["limits"] = limits if limits is not None else {"maxSteps": budgets.max_steps}
    # weftai types its options as a TypedDict; we assemble the mapping conditionally
    # because passing store=None is not the same as leaving it out.
    return create_runtime(cast("Any", options))


def plan_schema_for(registry: Registry[Any], policy: TurnPolicy | None = None) -> dict[str, Any]:
    """The JSON schema the model answers with.

    `maxSteps` is passed explicitly. weftai's tool binding leaves it out, so a model that is
    never told the cap discovers it by exceeding it — which costs a whole turn to learn
    something a single line of schema could have said.
    """
    steps = (policy or TurnPolicy()).max_steps
    schema: dict[str, Any] = registry.plan_schema({"maxSteps": steps})
    from lucy_api.turn.window import allow_show_from  # noqa: PLC0415 - turn imports packs

    return allow_show_from(schema)


__all__ = [
    "ALWAYS",
    "DEFER_ABOVE",
    "KEEP_RECENT",
    "SLOW_MULTIPLE",
    "SLOW_SERVICES",
    "apply_disabled",
    "build_registry",
    "build_runtime",
    "choose_bound",
    "limits_for",
    "plan_schema_for",
    "probe_all",
]
