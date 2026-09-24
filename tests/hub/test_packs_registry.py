"""A probe that fails is that capability's problem, and deferral is counted in packs."""

from __future__ import annotations

import asyncio
from typing import Any

from weftai import create_memory_store
from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.packs.base import Availability, Bound, Catalogue, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.registry import (
    ALWAYS,
    DEFER_ABOVE,
    FIRST_LOADED,
    KEEP_RECENT,
    PROBE_SECONDS,
    SLOW_MULTIPLE,
    _ceiling,
    build_registry,
    build_runtime,
    choose_bound,
    limits_for,
    probe_all,
)
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope


class Gadget:
    """A ready, hanging, or exploding pack used to drive the probe and deferral paths."""

    def __init__(
        self,
        pack_id: str,
        *,
        hang: bool = False,
        boom: bool = False,
        slow: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.id = pack_id
        self.title = pack_id.title()
        self.summary = f"The {pack_id} capability."
        self._hang = hang
        self._boom = boom
        self._slow = slow
        self._delay = delay

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Any, ...]:
        return ()

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        if self._hang:
            await asyncio.sleep(10)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._boom:
            raise RuntimeError("down")
        return Availability(state=State.ready, detail="connected")

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def run(_run: object) -> dict[str, bool]:
            return {"ok": True}

        return (
            define_operation(
                {
                    "name": f"{self.id}.ping",
                    "description": "Ping.",
                    "input": object_schema({}),
                    "output": value(object_schema({"ok": string_schema()})),
                    "effects": "read",
                    "run": run,
                }
            ),
        )


def _context() -> Any:
    return Capabilities((HelpPack(),)).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )


async def test_a_probe_that_times_out_or_raises_is_unavailable_not_a_failed_turn() -> None:
    context = _context()
    catalogue = await probe_all(
        (Gadget("slow", hang=True), Gadget("broken", boom=True), HelpPack()),
        context,
        seconds=0.05,
    )
    by_id = {item.pack.id: item.availability.state for item in catalogue.bound}
    assert by_id["slow"] is State.unavailable
    assert by_id["broken"] is State.unavailable
    assert by_id["help"] is State.ready
    slow = catalogue.get("slow")
    assert slow is not None
    assert "time" in slow.availability.detail
    broken = catalogue.get("broken")
    assert broken is not None
    assert "RuntimeError" in broken.availability.detail


async def test_deferral_holds_back_the_least_recent_ready_packs() -> None:
    packs = [HelpPack(), *[Gadget(f"g{index}") for index in range(DEFER_ABOVE)]]
    context = _context()
    catalogue = await probe_all(packs, context)
    bound, deferred = choose_bound(catalogue, recent=("g5", "g4"))
    names = [item.pack.id for item in bound]
    assert "help" in names
    assert "help" in ALWAYS
    assert "g5" in names
    assert deferred
    assert "g0" in deferred or "g1" in deferred or "g2" in deferred
    under = choose_bound(Catalogue(bound=catalogue.bound[:2]))
    assert under[1] == ()


def test_slow_capabilities_widen_the_plan_timeouts() -> None:
    help_bound = Bound(
        pack=HelpPack(),
        availability=Availability(state=State.ready),
        operations=(),
    )
    research = Bound(
        pack=Gadget("research"),
        availability=Availability(state=State.ready),
        operations=(),
    )
    ordinary = limits_for((help_bound,))
    slow = limits_for((help_bound, research))
    assert slow["stepTimeoutMs"] == ordinary["stepTimeoutMs"] * 3
    assert slow["planTimeoutMs"] == ordinary["planTimeoutMs"] * 3


def test_a_runtime_keeps_a_store_when_one_is_handed_over() -> None:
    registry = build_registry((), with_standard=False)
    defaulted = build_runtime(registry)
    stored = build_runtime(registry, create_memory_store(), limits={"maxSteps": 4})
    assert defaulted is not None
    assert stored is not None


async def test_an_always_on_capability_does_not_spend_the_recency_budget() -> None:
    """`ALWAYS` is documented as "never deferred" and `KEEP_RECENT` as "how many of them stay
    bound, most recently used first". Appending the always-on ones to `kept` charged them
    against that budget, so three of them ate three of the four slots — and on a stock family
    that deferred `workspace` and `watch` on every fresh session. A real model read the result
    and said its workspace was "listed as deferred, but the live state shows it attached this
    turn. I'm going with the live state" — which would have failed on the next step.
    """
    packs = [HelpPack(), *[Gadget(f"g{index}") for index in range(DEFER_ABOVE)]]
    catalogue = await probe_all(packs, _context())
    bound, deferred = choose_bound(catalogue, recent=())

    names = {item.pack.id for item in bound}
    assert "help" in names, "always-on, and not at the cost of a slot"
    gadgets = sorted(name for name in names if name.startswith("g"))
    assert len(gadgets) == KEEP_RECENT, gadgets
    assert len(deferred) == DEFER_ABOVE - KEEP_RECENT


async def test_a_new_conversation_keeps_the_most_useful_capabilities_first() -> None:
    """The bug, named: with no recency, the tie was broken by id, and on a stock family the
    capability left out of four slots was `workspace` -- last in the alphabet -- so every new
    conversation spent a round binding it before it could touch a file."""
    stock = ("notes", "research", "settings", "watch", "workspace", "agents", "work")
    catalogue = await probe_all([HelpPack(), *[Gadget(name) for name in stock]], _context())

    bound, deferred = choose_bound(catalogue, recent=())

    names = {item.pack.id for item in bound}
    assert {"notes", "workspace", "research", "watch"} <= names
    assert deferred == ("settings",)
    assert FIRST_LOADED[:KEEP_RECENT] == ("notes", "workspace", "research", "watch")


async def test_what_the_conversation_used_still_comes_before_the_default_order() -> None:
    stock = ("notes", "research", "settings", "watch", "workspace", "music")
    catalogue = await probe_all([HelpPack(), *[Gadget(name) for name in stock]], _context())

    _bound, deferred = choose_bound(catalogue, recent=("music", "settings"))

    assert set(deferred) == {"research", "watch"}


# --- the slow ones get the same allowance to answer that they get to work ----------------------
#
# `SLOW_SERVICES` widened step and plan timeouts and nothing else, so the capability declared
# to need longer was given the shortest possible leash on the one call that decides whether it
# is bound at all. Research's probe asks web-search for its providers, which warms a browser
# pool: the first such call after an idle spell measured 5.06s against a ceiling of 5.00, in
# front of a service that answered every later call in 47ms. What a person saw on the first
# turn of every session was "Research: it did not answer in time."


async def test_a_slow_capability_is_given_longer_to_answer_than_an_ordinary_one() -> None:
    """The defect in one line: the same delay, ready for research and down for anything else."""
    just_over = 0.05 * 1.4
    catalogue = await probe_all(
        (Gadget("research", delay=just_over), Gadget("notes", delay=just_over)),
        _context(),
        seconds=0.05,
    )
    by_id = {item.pack.id: item.availability.state for item in catalogue.bound}
    assert by_id["research"] is State.ready
    assert by_id["notes"] is State.unavailable


async def test_a_slow_capability_that_is_really_down_is_still_called_down() -> None:
    """Longer, not unbounded. A person waiting on a reply is not made to wait forever for a
    service that has stopped answering."""
    catalogue = await probe_all((Gadget("research", hang=True),), _context(), seconds=0.02)
    research = catalogue.get("research")
    assert research is not None
    assert research.availability.state is State.unavailable


def test_a_step_and_the_probe_in_front_of_it_are_widened_by_the_same_figure() -> None:
    """Two figures drift, and the way they drift is this bug. One constant, read twice."""
    ordinary = limits_for((Bound(pack=HelpPack(), availability=Availability(state=State.ready)),))
    slow = limits_for(
        (
            Bound(pack=HelpPack(), availability=Availability(state=State.ready)),
            Bound(pack=Gadget("research"), availability=Availability(state=State.ready)),
        )
    )
    assert slow["stepTimeoutMs"] == ordinary["stepTimeoutMs"] * SLOW_MULTIPLE
    assert _ceiling("research", PROBE_SECONDS) == PROBE_SECONDS * SLOW_MULTIPLE
    assert _ceiling("notes", PROBE_SECONDS) == PROBE_SECONDS
