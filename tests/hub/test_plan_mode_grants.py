"""Plan mode is read-only whatever was granted, and so is every helper.

The bug, named: a standing allow was read before the mode, so in plan mode a write the person
had once said yes to "for this conversation" ran anyway. Every helper runs in plan mode with
its parent's grants, so every helper in that conversation could write -- against the brief,
`agents.md`, `helpers.md` and the team skill, which all say a helper cannot.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.base import Availability, Bound, Catalogue, Permission, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.gate import Grant, PermissionGate
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore


def _operation(name: str, effects: str) -> Any:
    async def run(_context: object) -> dict[str, str]:
        return {"ok": "yes"}

    return define_operation(
        {
            "name": name,
            "description": f"{name}, for the gate.",
            "input": object_schema({}),
            "output": value(object_schema({"ok": string_schema()})),
            "effects": effects,
            "run": run,
        }
    )


PERMISSIONS = (
    Permission(
        id="gadget.change",
        title="Change the gadget",
        description="Writes.",
        risk="write",
        covers=("gadget.poke",),
    ),
    Permission(
        id="gadget.meter",
        title="Run the meter",
        description="Reads, and costs money.",
        risk="spend",
        covers=("gadget.measure",),
    ),
)


def catalogue() -> Catalogue:
    pack = SimpleNamespace(id="gadget", permissions=lambda: PERMISSIONS)
    operations = (_operation("gadget.poke", "write"), _operation("gadget.measure", "read"))
    bound = Bound(pack=pack, availability=Availability(state=State.ready), operations=operations)  # type: ignore[arg-type]
    return Catalogue(bound=(bound,))


def inspect(op: str, mode: str, grants: dict[str, Grant]) -> Any:
    plan = {"steps": [{"id": "s", "op": op, "input": {}}]}
    return PermissionGate().inspect(plan, mode=mode, grants=grants, catalogue=catalogue())


ALLOWED = {
    "gadget.change": Grant("gadget.change", "allow", "personal"),
    "gadget.meter": Grant("gadget.meter", "allow", "personal"),
}


def test_a_write_in_plan_mode_is_refused_even_under_a_standing_yes() -> None:
    verdict = inspect("gadget.poke", "plan", ALLOWED)
    assert not verdict.allowed
    assert verdict.denied, "refused outright, not parked for an approval plan mode never gives"
    assert verdict.message == "Change the gadget is a write; plan mode is read-only."


def test_the_same_yes_still_runs_the_write_outside_plan_mode() -> None:
    assert inspect("gadget.poke", "ask", ALLOWED).allowed


def test_a_read_that_spends_keeps_its_grant_in_plan_mode() -> None:
    assert inspect("gadget.measure", "plan", ALLOWED).allowed
    assert not inspect("gadget.measure", "plan", {}).allowed, "and without one it is refused"


def test_a_deny_still_reads_as_the_person_s_own_refusal_outside_plan_mode() -> None:
    denied = {"gadget.change": Grant("gadget.change", "deny", "personal", instruction="Not now.")}
    verdict = inspect("gadget.poke", "ask", denied)
    assert verdict.denied
    assert verdict.message == "Not now."


async def test_a_helper_cannot_write_under_the_grant_its_parent_was_given(
    sessions_store: SessionStore,
) -> None:
    """End to end: the person said yes to the write for Lucy; a helper Lucy started tried it."""
    from test_packs_registry import Gadget

    ran: list[str] = []

    class Writer(Gadget):
        def permissions(self) -> tuple[Any, ...]:
            return PERMISSIONS

        def operations(self, _context: object) -> tuple[Any, ...]:
            async def poke(_run: object) -> dict[str, str]:
                ran.append("poked")
                return {"ok": "yes"}

            return (
                define_operation(
                    {
                        "name": "gadget.poke",
                        "description": "Change the gadget.",
                        "input": object_schema({}),
                        "output": value(object_schema({"ok": string_schema()})),
                        "effects": "write",
                        "run": poke,
                    }
                ),
            )

    created = await sessions_store.create(
        "acct_plan", CreateSession(model="scripted:demo"), "plan-key"
    )
    provider = ScriptedProvider(
        [plans({"steps": [{"id": "w", "op": "gadget.poke", "input": {}}]}), speaks("Done.")]
    )
    capabilities = Capabilities((HelpPack(), AgentsPack(), Writer("gadget")))
    child = ChildRuntime(
        sessions_store,
        AgentStore(sessions_store),
        ModelRegistry({"scripted": lambda _model: provider}),
        capabilities,
    )
    capabilities.child = child
    parent = capabilities.context_for(
        SessionScope(account_id="acct_plan", profile="personal", session_id=str(created["id"]))
    )
    parent.grants = dict(ALLOWED)

    await child.run(parent, objective="Tidy the gadget.", role="reader")

    assert ran == [], "the write never ran"
    told = " ".join(str(message.content) for message in provider.requests[1].messages)
    assert "plan mode is read-only" in told


async def test_a_helper_may_claim_a_journal_task_in_plan_mode() -> None:
    """The bug, named: journal.claim and journal.complete fell under "Start a helper", a write,
    so a helper -- always in plan mode -- was refused them, and the main thread would have asked
    the person "Start a helper?" to claim a task. Nobody could use them."""
    from lucy_api.packs.agents import AgentsPack

    [delegate] = AgentsPack().permissions()
    assert "journal.claim" not in delegate.covers
    assert "journal.complete" not in delegate.covers
