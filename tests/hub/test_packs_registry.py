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
    KEEP_RECENT,
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
    ) -> None:
        self.id = pack_id
        self.title = pack_id.title()
        self.summary = f"The {pack_id} capability."
        self._hang = hang
        self._boom = boom
        self._slow = slow

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
