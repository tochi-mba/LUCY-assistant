"""References survive from one plan to the next, because the schema says they do.

The plan schema's own hint reads "Split the work across calls; results stay available by
name" -- and the store they stayed in was new for every plan, so `$find` from the last plan
was an invalid reference and the model was told the *name* was wrong. Seen live three times
in one day, as "play Worship by Asake" failing twice in front of the person.
"""

from __future__ import annotations

from typing import Any

from weftai.operation import define_operation
from weftai.schema.ref import ref
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.collections import NOTE
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.gate import Grant
from lucy_api.sessions.scope import SessionScope

NOTES = [
    {"id": "m1", "title": "Tea", "body": "Earl Grey", "kind": "fact", "trust": "stated"},
    {"id": "m2", "title": "Gig", "body": "Tuesday", "kind": "episode", "trust": "observed"},
]


class Jukebox:
    """One pack with a search that returns a collection and a play that resolves a reference."""

    id = "juke"
    title = "Jukebox"
    summary = "Finds and confirms notes."

    def __init__(self) -> None:
        self.played: list[Any] = []

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Permission, ...]:
        return (
            Permission(
                id="juke.play",
                title="Play something",
                description="Confirm one found entry.",
                risk="write",
                covers=("juke.play",),
            ),
        )

    def result_trust(self, operation: str, data: object) -> Any:
        from lucy_api.context.types import Trust

        del operation, data
        return Trust.observed

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: Any) -> Availability:
        del context
        return Availability(state=State.ready)

    def operations(self, context: Any) -> tuple[Any, ...]:
        del context

        async def search(_run: Any) -> list[dict[str, Any]]:
            return NOTES

        async def play(run: Any) -> dict[str, Any]:
            self.played.append(run.input["entry"])
            return {"ok": "yes"}

        return (
            define_operation(
                {
                    "name": "juke.search",
                    "description": "Find entries.",
                    "input": object_schema({"query": string_schema()}),
                    "output": NOTE,
                    "effects": "read",
                    "run": search,
                }
            ),
            define_operation(
                {
                    "name": "juke.play",
                    "description": "Confirm one entry.",
                    "input": object_schema({"entry": ref(NOTE)}),
                    "output": value(object_schema({"ok": string_schema()})),
                    "effects": "write",
                    "run": play,
                }
            ),
        )


def _wired(session_id: str = "ses_a") -> tuple[Jukebox, Capabilities, Any]:
    pack = Jukebox()
    capabilities = Capabilities((pack,))
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a", profile="personal", session_id=session_id, permission_mode="auto"
        )
    )
    context.grants["juke.play"] = Grant("juke.play", "allow", "*")
    return pack, capabilities, context


def _search_plan() -> dict[str, Any]:
    return {"steps": [{"id": "found", "op": "juke.search", "input": {"query": "tea"}}]}


def _play_plan() -> dict[str, Any]:
    return {"steps": [{"id": "play", "op": "juke.play", "input": {"entry": "$found[2]"}}]}


async def test_a_reference_to_the_last_plan_s_step_resolves() -> None:
    """The bug, named: "play Worship by Asake" found the track in one plan and played it in
    the next; the second plan's `$found` was refused as an unknown name, twice, in front of
    the person."""
    pack, capabilities, context = _wired()
    await capabilities.probe(context)

    first = await capabilities.execute(_search_plan(), context)
    assert not first.get("issues")

    second = await capabilities.execute(_play_plan(), context)
    assert not second.get("issues"), second
    [ran] = second["steps"]
    assert ran["data"]["ok"] == "yes"
    [resolved] = pack.played
    assert resolved.items[0]["id"] == "m2", "the second entry of the earlier plan's result"


async def test_the_gate_sees_the_stored_result_the_executor_will_resolve() -> None:
    """In `ask` mode the write referencing a stored result must park for approval -- not be
    refused as an invalid plan the gate never inspected."""
    pack, capabilities, context = _wired()
    context.permission_mode = "ask"
    context.grants.clear()
    await capabilities.probe(context)

    first = await capabilities.execute(_search_plan(), context)
    assert not first.get("issues")

    second = await capabilities.execute(_play_plan(), context)
    [issue] = second["issues"]
    assert issue["code"] == "permission_required", second
    assert pack.played == [], "nothing ran without the person's yes"


async def test_another_session_cannot_name_this_session_s_results() -> None:
    pack, capabilities, context = _wired()
    await capabilities.probe(context)
    assert not (await capabilities.execute(_search_plan(), context)).get("issues")

    other = capabilities.context_for(
        SessionScope(
            account_id="acct_a", profile="personal", session_id="ses_b", permission_mode="auto"
        )
    )
    other.grants["juke.play"] = Grant("juke.play", "allow", "*")
    await capabilities.probe(other)

    refused = await capabilities.execute(_play_plan(), other)
    [issue] = refused["issues"]
    assert "found" in issue["message"], "the other session's name is not known here"
    assert pack.played == []
