"""Every log line says which conversation, turn and helper it is about, and each step is timed.

The application log carried eleven correlation fields on every line, and three of them --
`session_id`, `turn_id`, `agent_id` -- were null on every line ever written: `bind()` was
defined and never called. `operation`, `duration_ms` and `outcome` were null too, because
`operation()` was never called either. So a person debugging one slow or failing conversation
could not pick its lines out of the log, and no line said which call had been slow.
"""

from __future__ import annotations

import io
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from weftai.operation import define_operation
from weftai.schema.spec import object_schema
from weftai.schema.types import value

from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore
from lucy_api.core.logging import JsonFormatter, bind
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.base import Availability, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.packs.steplog import step_hooks
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Mapping
    from pathlib import Path

ACCOUNT = "acct_logs"
READ_DOCS = {"steps": [{"id": "docs", "op": "help.docs", "input": {"topic": "notes"}}]}
BREAK = {"steps": [{"id": "boom", "op": "broken.go", "input": {}}]}


class Broken:
    """A capability whose one operation fails, with a message that must not be logged."""

    id = "broken"
    title = "Broken"
    summary = "Fails."
    docs = None

    def permissions(self) -> tuple[()]:
        return ()

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready)

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def go(_run: object) -> dict[str, bool]:
            message = "the value that caused it: 4111-1111"
            raise RuntimeError(message)

        return (
            define_operation(
                {
                    "name": "broken.go",
                    "description": "Go.",
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "run": go,
                }
            ),
        )


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


@pytest.fixture
def lines() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """A reader for the JSON lines the hub has written since the test began."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    def read() -> list[dict[str, Any]]:
        return [json.loads(line) for line in stream.getvalue().splitlines() if line]

    try:
        yield read
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def _a_turn(store: SessionStore, *script: Any) -> tuple[str, str]:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "key")
    conversation = str(created["id"])
    queued = await submit_messages(
        store, ACCOUNT, conversation, [{"type": "input.message", "content": "go"}], "q"
    )
    running = TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _model: ScriptedProvider(list(script))}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities=Capabilities((HelpPack(), Broken())),
    )
    running.wake()
    await running.join()
    await running.aclose()
    return conversation, str(queued["id"])


async def test_a_turn_s_lines_say_which_conversation_and_turn_they_are_about(
    store: SessionStore, lines: Callable[[], list[dict[str, Any]]]
) -> None:
    """The bug, named: these two fields were null on every line ever written."""
    conversation, turn = await _a_turn(store, plans(READ_DOCS), speaks("Done."))
    written = lines()

    ours = [line for line in lines() if line["turn_id"] == turn]
    assert ours, written
    assert {line["session_id"] for line in ours} == {conversation}
    ended = [line for line in ours if line["message"] == "turn_ended"]
    assert len(ended) == 1
    assert ended[0]["duration_ms"] >= 0


async def test_each_step_is_one_line_with_its_operation_time_and_outcome(
    store: SessionStore, lines: Callable[[], list[dict[str, Any]]]
) -> None:
    await _a_turn(store, plans(READ_DOCS), plans(BREAK), speaks("Done."))

    steps = [line for line in lines() if line["message"] == "step"]
    by_operation = {line["operation"]: line for line in steps}
    ran = by_operation["help.docs"]
    assert (ran["capability"], ran["outcome"], ran["level"]) == ("help", "ok", "INFO")
    assert ran["duration_ms"] >= 0
    failed = by_operation["broken.go"]
    assert (failed["outcome"], failed["error_type"], failed["level"]) == (
        "error",
        "RuntimeError",
        "WARNING",
    )
    assert "4111" not in json.dumps(steps), "an error's message is never logged"


async def test_a_helper_s_lines_carry_its_own_id_and_the_conversation_s(
    store: SessionStore, lines: Callable[[], list[dict[str, Any]]]
) -> None:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "helper-key")
    session = str(created["id"])
    provider = ScriptedProvider([plans(READ_DOCS), speaks("Read it.")])
    capabilities = Capabilities((HelpPack(), AgentsPack()))
    child = ChildRuntime(
        store, AgentStore(store), ModelRegistry({"scripted": lambda _m: provider}), capabilities
    )
    context = capabilities.context_for(
        SessionScope(account_id=ACCOUNT, profile="personal", session_id=session)
    )

    with bind(session_id=session, turn_id="trn_parent"):
        result = await child.run(context, objective="Read the docs", role="reader")

    [step] = [line for line in lines() if line["message"] == "step"]
    assert (step["agent_id"], step["session_id"], step["turn_id"]) == (
        result["agent_id"],
        session,
        "trn_parent",
    )
    assert step["parent_agent_id"] is None, "the conversation itself started it"


def test_a_hook_given_odd_arguments_writes_nothing_and_raises_nothing(
    lines: Callable[[], list[dict[str, Any]]],
) -> None:
    """weftai calls `afterStep` inside the step's own try: a hook that raised would turn a
    step that succeeded into one that failed."""
    hooks = step_hooks()
    hooks["beforeStep"]({})
    hooks["afterStep"]({})
    hooks["onStepError"]({"step": object(), "error": ValueError("x")})
    assert lines() == []
