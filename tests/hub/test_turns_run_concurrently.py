"""Turns of different conversations run at once; turns of one conversation never do.

Measured on the live hub (2026-10-10): a message to a fresh conversation was accepted in
0.12 s and its turn started 56.5 s later. The supervisor claimed one turn and ran it to the
end before it claimed the next, so every conversation's turn, whoever's it was, waited
behind every other running turn: a background tester's, a woken one, anything (ADR-0019).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from typing import TYPE_CHECKING, Any

import pytest
from conftest import build_settings

from lucy_api.context.build import Live
from lucy_api.core.config import MAX_CONCURRENT_TURNS
from lucy_api.core.container import build_container
from lucy_api.core.logging import JsonFormatter
from lucy_api.model.registry import ModelRegistry
from lucy_api.model.types import Chunk, Reply
from lucy_api.model.wire import CHUNK_DONE
from lucy_api.packs.help import HelpPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.settings.policy import TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

ACCOUNT = "acct_concurrent_turns"
WAIT = 5.0
"""Seconds a test waits for something that, working, happens at once."""


class _Snapshot:
    async def snapshot(self, session_id: str) -> Mapping[str, Any]:
        return {"session_id": session_id}


class Held:
    """A model that answers at once, except to a message with "hold" in it.

    Those wait for `release`. `holding` is set once `expected` of them are waiting, which is
    how a test knows that many turns are running at the same moment.
    """

    name = "scripted"

    def __init__(self, *, expected: int = 1) -> None:
        self.expected = expected
        self.holding = asyncio.Event()
        self.release = asyncio.Event()
        self.waiting = 0
        self.requests: dict[str, Any] = {}
        """Each request, by what the person said in it."""
        self.asked: list[str] = []
        """What the person said, in the order the model was asked."""

    async def complete(self, request: Any) -> Reply:
        return await self._answer(request)

    async def stream(self, request: Any) -> AsyncIterator[Chunk]:
        yield Chunk(kind=CHUNK_DONE, reply=await self._answer(request))

    async def _answer(self, request: Any) -> Reply:
        said = next(m.content for m in reversed(request.messages) if m.content != "")
        self.asked.append(said)
        self.requests[said] = request
        if "hold" in said:
            self.waiting += 1
            if self.waiting >= self.expected:
                self.holding.set()
            await self.release.wait()
        return Reply(text=f"answered: {said}")


class Claims(SessionStore):
    """The real store, noting each turn it hands out, and able to act once it finds none."""

    def __init__(self, worker: SqlWorker) -> None:
        super().__init__(worker)
        self.claimed: list[str] = []
        self.on_nothing: Callable[[], Awaitable[None]] | None = None

    async def claim_next_turn(self) -> dict[str, Any] | None:
        row = await super().claim_next_turn()
        if row is not None:
            self.claimed.append(str(row["id"]))
        elif self.on_nothing is not None:
            act, self.on_nothing = self.on_nothing, None
            await act()
        return row


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Claims]:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = Claims(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def conversation(store: SessionStore, said: str) -> tuple[str, str]:
    """A new conversation with one queued message: its id, and its turn's."""
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), f"new-{said}")
    session = str(created["id"])
    turn = await submit_messages(
        store, ACCOUNT, session, [{"type": "input.message", "content": said}], f"say-{said}"
    )
    return session, str(turn["id"])


def supervisor(
    store: SessionStore,
    model: Held,
    *,
    on_status: Any = None,
    max_concurrent: int = MAX_CONCURRENT_TURNS,
) -> TurnSupervisor:
    return TurnSupervisor(
        store,
        ModelRegistry({"scripted": lambda _: model}),
        EventEmitter(SqlEventLog(store), _Snapshot()),
        capabilities=Capabilities((HelpPack(),)),
        on_status=on_status,
        max_concurrent=max_concurrent,
    )


async def status(store: SessionStore, turn: str) -> str:
    return str((await store.turn(ACCOUNT, turn))["status"])


async def test_a_conversation_s_turn_does_not_wait_for_another_conversation_s(
    store: Claims,
) -> None:
    """The bug, named: a message to a fresh conversation was accepted in 0.12 s and its turn
    started 56.5 s later. The supervisor ran one turn at a time across every conversation,
    so this one waited until somebody else's turn had finished."""
    model = Held()
    busy, busy_turn = await conversation(store, "hold this one")
    fresh, fresh_turn = await conversation(store, "hello")
    finished = {busy: asyncio.Event(), fresh: asyncio.Event()}

    async def on_status(_account: str, session: str, _turn: str, _status: str) -> None:
        finished[session].set()

    running = supervisor(store, model, on_status=on_status)
    running.wake()
    await asyncio.wait_for(model.holding.wait(), WAIT)
    await asyncio.wait_for(finished[fresh].wait(), WAIT)

    assert await status(store, fresh_turn) == "completed"
    assert await status(store, busy_turn) == "running"
    model.release.set()
    await running.join()
    assert await status(store, busy_turn) == "completed"
    await running.aclose()


async def test_past_the_cap_a_turn_waits_and_takes_the_first_slot_freed(store: Claims) -> None:
    model = Held()
    _busy, busy_turn = await conversation(store, "hold this one")
    _later, later_turn = await conversation(store, "hello")
    running = supervisor(store, model, max_concurrent=1)

    running.wake()
    await asyncio.wait_for(model.holding.wait(), WAIT)
    assert await status(store, later_turn) == "queued"
    model.release.set()
    await running.join()

    assert await status(store, busy_turn) == "completed"
    assert await status(store, later_turn) == "completed"
    assert model.asked == ["hold this one", "hello"]
    await running.aclose()


async def test_turns_are_claimed_in_the_order_they_were_queued(store: Claims) -> None:
    model = Held()
    turns = [(await conversation(store, said))[1] for said in ("one", "two", "three")]
    running = supervisor(store, model, max_concurrent=2)

    running.wake()
    await running.join()

    assert store.claimed == turns
    assert [await status(store, turn) for turn in turns] == ["completed"] * 3
    await running.aclose()


async def test_a_wake_while_the_store_is_being_asked_is_not_lost(store: Claims) -> None:
    """The claimer asked, the store said nothing was queued, and in between a turn was queued
    and `wake()` called. That call found the claimer still running and so did nothing; the
    claimer then stopped, and the new turn would have sat queued until something else woke
    the supervisor."""
    model = Held()
    late: list[str] = []
    running = supervisor(store, model)

    async def queue_one() -> None:
        late.append((await conversation(store, "just in time"))[1])
        running.wake()

    store.on_nothing = queue_one
    running.wake()
    await running.join()

    assert late
    assert await status(store, late[0]) == "completed"
    await running.aclose()


async def test_each_running_turn_takes_the_authority_prepared_for_it(store: Claims) -> None:
    model = Held(expected=2)
    running = supervisor(store, model)
    for said, ceiling in (("hold a", 1111), ("hold b", 2222)):
        session, turn = await conversation(store, said)
        context = Capabilities((HelpPack(),)).context_for(
            SessionScope(account_id=ACCOUNT, profile="personal", session_id=session)
        )
        context.policy = TurnPolicy(
            max_output_tokens=ceiling, auto_title=False, notify_on_long_turn=False
        )
        running.authorize(turn, PreparedTurn(pack_context=context, live=Live()))

    running.wake()
    await asyncio.wait_for(model.holding.wait(), WAIT)
    model.release.set()
    await running.join()

    asked = {said: request.max_output_tokens for said, request in model.requests.items()}
    assert asked == {"hold a": 1111, "hold b": 2222}
    await running.aclose()


async def test_turns_running_at_once_each_log_under_their_own_ids(store: Claims) -> None:
    written = io.StringIO()
    handler = logging.StreamHandler(written)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        model = Held(expected=2)
        first, first_turn = await conversation(store, "hold a")
        second, second_turn = await conversation(store, "hold b")
        running = supervisor(store, model)
        running.wake()
        await asyncio.wait_for(model.holding.wait(), WAIT)
        model.release.set()
        await running.join()
        await running.aclose()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)

    lines = [json.loads(line) for line in written.getvalue().splitlines() if line]
    ended = {
        line["turn_id"]: line["session_id"] for line in lines if line["message"] == "turn_ended"
    }
    assert ended == {first_turn: first, second_turn: second}


async def test_a_turn_that_breaks_is_logged_and_the_next_one_still_runs(
    store: Claims, caplog: pytest.LogCaptureFixture
) -> None:
    """One turn's bug used to end the drain loop, so every turn queued behind it waited for
    the next wake. Now that turns share the process it must not reach the others at all."""
    model = Held()
    broken, _broken_turn = await conversation(store, "first")
    _after, after_turn = await conversation(store, "second")

    async def on_status(_account: str, session: str, _turn: str, _status: str) -> None:
        if session == broken:
            message = "a status listener with a bug in it"
            raise RuntimeError(message)

    running = supervisor(store, model, on_status=on_status, max_concurrent=1)
    with caplog.at_level(logging.ERROR, logger="lucy_api.turn.supervisor"):
        running.wake()
        await running.join()

    assert await status(store, after_turn) == "completed"
    crashed = [record for record in caplog.records if record.getMessage() == "turn_crashed"]
    assert len(crashed) == 1
    assert crashed[0].exc_info is not None
    await running.aclose()


async def test_closing_stops_every_running_turn_and_the_next_start_fails_them(
    store: Claims,
) -> None:
    model = Held(expected=2)
    turns = [(await conversation(store, said))[1] for said in ("hold a", "hold b")]
    running = supervisor(store, model)
    running.wake()
    await asyncio.wait_for(model.holding.wait(), WAIT)

    await running.aclose()
    assert [await status(store, turn) for turn in turns] == ["running", "running"]
    running.wake()
    assert store.claimed == turns

    restarted = supervisor(store, Held())
    await restarted.start()
    await restarted.join()
    ended = [await store.turn(ACCOUNT, turn) for turn in turns]
    assert [row["status"] for row in ended] == ["failed", "failed"]
    assert {row["stop_reason"] for row in ended} == {"process_restarted"}
    await restarted.aclose()


async def test_a_cap_below_one_is_refused_rather_than_never_running_anything(
    store: Claims,
) -> None:
    with pytest.raises(ValueError, match="LUCY_MAX_CONCURRENT_TURNS"):
        supervisor(store, Held(), max_concurrent=0)


async def test_the_cap_is_a_setting_and_reaches_the_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert build_settings().max_concurrent_turns == MAX_CONCURRENT_TURNS
    monkeypatch.setenv("LUCY_MAX_CONCURRENT_TURNS", "2")
    container = build_container(build_settings())
    try:
        assert container.turns._max_concurrent == 2
    finally:
        await container.aclose()
