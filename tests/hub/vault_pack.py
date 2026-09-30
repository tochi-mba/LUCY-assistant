"""Vault: a small pack for approval tests, shared so each module reads as its own story.

A read, a gated fetch, a destructive play that takes a reference, and a gated mark: every
shape a held, re-parked or refused call can take is reachable from these four.
"""

from __future__ import annotations

from typing import Any

from weftai import collection
from weftai.operation import define_operation
from weftai.schema import ref
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Permission, State

RECORD = collection("records", dict[str, Any], label=lambda record: str(record["name"]))


def record(name: str) -> dict[str, Any]:
    return {"name": name, "uri": f"vault:{name}"}


class Vault:
    """Records to find, fetch, play and mark; what ran is kept for the test to read."""

    id = "vault"
    title = "Vault"
    summary = "Records to find, fetch, play and mark."

    def __init__(self) -> None:
        self.fetched: list[str] = []
        self.played: list[list[str]] = []
        self.marked: list[str] = []

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[Permission, ...]:
        return (
            Permission(
                id="vault.take",
                title="Fetch records",
                description="Fetch a record from storage.",
                risk="write",
                covers=("vault.fetch",),
            ),
            Permission(
                id="vault.play",
                title="Play records",
                description="Play records.",
                risk="destructive",
                covers=("vault.play",),
            ),
            Permission(
                id="vault.mark",
                title="Mark records",
                description="Mark a record.",
                risk="write",
                covers=("vault.mark",),
            ),
        )

    def result_trust(self, operation: str, data: object) -> Trust:
        del operation, data
        return Trust.observed

    def setup(self) -> None:
        return None

    async def probe(self, context: object) -> Availability:
        del context
        return Availability(state=State.ready, detail="always")

    def operations(self, context: object) -> tuple[Any, ...]:
        del context

        async def find(ctx: Any) -> list[dict[str, Any]]:
            return [record(str(ctx.input["name"]))]

        async def fetch(ctx: Any) -> list[dict[str, Any]]:
            self.fetched.append(str(ctx.input["name"]))
            return [record(str(ctx.input["name"]))]

        async def play(ctx: Any) -> dict[str, Any]:
            self.played.append([str(item["uri"]) for item in ctx.input["record"].items])
            return {}

        async def mark(ctx: Any) -> dict[str, Any]:
            self.marked.append(str(ctx.input["name"]))
            return {}

        named = object_schema({"name": string_schema()})
        return (
            define_operation(
                {
                    "name": "vault.find",
                    "description": "Find a record.",
                    "input": named,
                    "output": RECORD,
                    "run": find,
                }
            ),
            define_operation(
                {
                    "name": "vault.fetch",
                    "description": "Fetch a record from storage.",
                    "input": named,
                    "output": RECORD,
                    "effects": "write",
                    "run": fetch,
                }
            ),
            define_operation(
                {
                    "name": "vault.play",
                    "description": "Play records.",
                    "input": object_schema({"record": ref(RECORD)}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": play,
                }
            ),
            define_operation(
                {
                    "name": "vault.mark",
                    "description": "Mark a record.",
                    "input": named,
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": mark,
                }
            ),
        )


__all__ = ["RECORD", "Vault", "record"]
