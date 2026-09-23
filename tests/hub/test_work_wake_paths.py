"""Who asks to wake the session, and who does not.

A command run with `wake: true`, and a helper the main thread started, both carry the
account and the wake flag into the registry. A helper's own helper does not: its parent is
still running, and its parent is the one that will read it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from lucy_api.clients.environments import Environment, FakeEnvironmentsClient, Ran
from lucy_api.packs.agents import AgentsPack, _spawn
from lucy_api.packs.service import Capabilities
from lucy_api.packs.workspace import WorkspacePack
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.work import Kind, Registry


class Helpers:
    """A child runtime that prepares instantly and answers with one line."""

    def __init__(self) -> None:
        self.prepared = 0

    async def prepare(self, parent: Any, **_: Any) -> tuple[str, int]:
        self.prepared += 1
        return f"agt_{self.prepared}", self.prepared

    async def run(self, parent: Any, **_: Any) -> dict[str, Any]:
        return {"summary": "found it"}

    async def discard_setup(self, parent: Any, agent_id: str, task_id: int) -> None:
        return None

    async def send(self, parent: Any, agent_id: str, body: str) -> dict[str, Any]:
        return {}

    async def reopen(
        self, parent: Any, agent_id: str, *, return_schema: str = ""
    ) -> dict[str, Any]:
        return {}

    async def read_journal(self, parent: Any) -> dict[str, Any]:
        return {}

    async def claim(self, parent: Any, task_id: str) -> dict[str, Any]:
        return {}

    async def complete(self, parent: Any, task_id: str) -> dict[str, Any]:
        return {}


def scope(**overrides: Any) -> SessionScope:
    fields: dict[str, Any] = {
        "account_id": "acct-a",
        "profile": "personal",
        "session_id": "sess-a",
        "workspace": WorkspaceScope("env-1", "sess-a", ready=True),
        "permission_mode": "auto",
    }
    return SessionScope(**{**fields, **overrides})


async def test_a_command_run_with_wake_asks_to_be_woken_on_this_persons_behalf() -> None:
    fake = FakeEnvironmentsClient()
    fake.seed(Environment("env-1", "Conversation", profile="personal"))
    fake.script("build", Ran(command="build", exit_code=0, output="ok", state="idle"))
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)], work=work)
    context = capabilities.context_for(scope())
    await capabilities.probe(context)
    try:
        plan = {
            "steps": [
                {
                    "id": "a",
                    "op": "workspace.run",
                    "input": {"command": "build", "wait": False, "wake": True},
                },
                {"id": "b", "op": "workspace.run", "input": {"command": "build", "wait": False}},
            ]
        }
        result = await capabilities.execute(plan, context)
        asked, quiet = (step["data"]["work_id"] for step in result["steps"])
        finished = {record.id: record for record in work._records.values()}
        assert finished[asked].wake is True
        assert finished[asked].account_id == "acct-a"
        assert finished[quiet].wake is False
    finally:
        await work.shutdown()


async def test_a_helper_started_by_the_main_thread_wakes_and_a_helpers_helper_does_not() -> None:
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities((AgentsPack(),), work=work)
    context = capabilities.context_for(scope())
    context.child = Helpers()
    try:
        top = await _spawn(work, context, depth=0, objective="Find the date", role="researcher")
        nested = await _spawn(work, context, depth=1, objective="Check it", role="checker")
        await work.wait(top["id"], 5)
        await work.wait(nested["id"], 5)
        records = {record.id: record for record in work._records.values()}
        assert records[top["id"]].kind is Kind.helper
        assert records[top["id"]].wake is True
        assert records[top["id"]].account_id == "acct-a"
        assert records[nested["id"]].wake is False
    finally:
        await work.shutdown()
