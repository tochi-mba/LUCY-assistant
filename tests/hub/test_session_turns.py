"""A turn ends once, and a stop only ever reaches the turn it names.

The interesting cases here are all races written down as ordinary calls.

A cancel that arrives after a turn finished must not reopen it, and must not reach forward
to the turn that started next -- so the terminal check and the write happen in one
transaction against the turn's own id, and the test for it cancels a finished turn while a
second one is live and then asserts the second is untouched.

A cancel of something *running* is a request rather than an execution, because the loop is
holding partial work and killing it from the outside throws that away. A cancel of anything
else is done on the spot: nothing is in flight, and a queued turn left waiting for a loop
that will never look at it again is a spinner nobody can stop.

Handing the session back to the person is conditional for the same reason. Cancelling one
queued turn while another is running must not tell every client the session went idle.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import CreateSession, Cursor, Outcome
from lucy_api.sessions.sql_store import identifier
from lucy_api.sessions.turns import cancel_turn, close_turn, list_turns, open_turn

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore

OWNER = "acct_owner"
STRANGER = "acct_stranger"
SPOKE = {"events": [{"type": "input.message", "content": "hello"}]}


async def a_session(store: SessionStore, account: str = OWNER, **fields: object) -> str:
    created = await store.create(account, CreateSession(**fields), identifier("key"))
    return str(created["id"])


async def a_running_turn(store: SessionStore, session: str) -> str:
    turn = await open_turn(store, OWNER, session, SPOKE)
    await close_turn(store, OWNER, str(turn["id"]), Outcome("running"))
    return str(turn["id"])


async def session_status(store: SessionStore, session: str) -> str:
    return str((await store.get(OWNER, session))["status"])


async def event_types(store: SessionStore, session: str) -> list[str]:
    return [str(event["type"]) for event in await store.records(OWNER, session, "events")]


async def test_an_opened_turn_is_queued_and_marks_the_session_as_working(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    turn = await open_turn(sessions_store, OWNER, session, SPOKE)

    assert turn["status"] == "queued"
    assert turn["input"] == SPOKE
    assert turn["cancel_requested"] == 0
    assert await session_status(sessions_store, session) == "queued"
    assert "lucy.turn.created" in await event_types(sessions_store, session)


async def test_opening_a_turn_in_another_accounts_session_finds_nothing(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)

    with pytest.raises(LucyError) as caught:
        await open_turn(sessions_store, STRANGER, session, SPOKE)

    assert caught.value.code == "not-found"
    assert await sessions_store.records(OWNER, session, "turns") == []


async def test_a_second_turn_queues_behind_the_first_and_says_what_it_is_behind(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    first = await a_running_turn(sessions_store, session)

    second = await open_turn(sessions_store, OWNER, session, SPOKE)

    assert second["status"] == "queued"
    queued = [
        event
        for event in await sessions_store.records(OWNER, session, "events")
        if event["type"] == "lucy.turn.created" and event["turn_id"] == second["id"]
    ]
    assert queued[0]["data"] == {"input_policy": "enqueue", "queued_behind": first}


async def test_a_session_whose_policy_is_reject_refuses_a_second_turn(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store, input_policy="reject")
    await a_running_turn(sessions_store, session)

    with pytest.raises(LucyError) as caught:
        await open_turn(sessions_store, OWNER, session, SPOKE)

    assert caught.value.status == 409
    # The refusal names the setting to change rather than saying "busy".
    assert "input_policy" in str(caught.value)
    assert len(await sessions_store.records(OWNER, session, "turns")) == 1


async def test_a_rejecting_session_takes_a_new_turn_once_the_last_one_ended(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store, input_policy="reject")
    first = await a_running_turn(sessions_store, session)
    await close_turn(sessions_store, OWNER, first, Outcome("completed", "success", "end_turn"))

    second = await open_turn(sessions_store, OWNER, session, SPOKE)

    assert second["status"] == "queued"


async def test_a_turn_cannot_be_moved_to_a_status_that_does_not_exist(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)

    with pytest.raises(ValueError, match="finished") as caught:
        await close_turn(sessions_store, OWNER, str(turn["id"]), Outcome("finished"))

    # The message lists what it should have been, rather than only saying no.
    assert "auth_required" in str(caught.value)


async def test_a_completed_turn_keeps_the_hubs_reason_and_the_providers_apart(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)

    closed = await close_turn(
        sessions_store,
        OWNER,
        str(turn["id"]),
        Outcome("failed", "error_max_iterations", "end_turn"),
    )

    assert closed["status"] == "failed"
    assert closed["termination"] == "error_max_iterations"
    assert closed["stop_reason"] == "end_turn"
    assert closed["finished_at"] is not None


async def test_a_turn_that_already_ended_ignores_a_second_verdict(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)
    await close_turn(sessions_store, OWNER, str(turn["id"]), Outcome("cancelled"))

    again = await close_turn(
        sessions_store, OWNER, str(turn["id"]), Outcome("completed", "success")
    )

    assert again["status"] == "cancelled"
    assert again["termination"] is None


async def test_cancelling_a_queued_turn_ends_it_on_the_spot(
    sessions_store: SessionStore,
) -> None:
    # Nothing is in flight, and a queued turn nobody will look at again is a spinner that
    # never stops.
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)

    cancelled = await cancel_turn(sessions_store, OWNER, str(turn["id"]))

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] == 1
    assert cancelled["finished_at"] is not None
    assert await session_status(sessions_store, session) == "idle"
    assert "lucy.turn.cancelled" in await event_types(sessions_store, session)


async def test_cancelling_a_turn_waiting_on_an_approval_also_ends_it_at_once(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)
    await close_turn(sessions_store, OWNER, str(turn["id"]), Outcome("input_required"))

    cancelled = await cancel_turn(sessions_store, OWNER, str(turn["id"]))

    assert cancelled["status"] == "cancelled"


async def test_cancelling_a_running_turn_asks_rather_than_kills(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)

    asked = await cancel_turn(sessions_store, OWNER, turn)

    # Still running, and now carrying the flag the loop reads. The work it has already done
    # is the loop's to finish with.
    assert asked["status"] == "running"
    assert asked["cancel_requested"] == 1
    assert await session_status(sessions_store, session) == "running"
    assert "lucy.turn.cancel_requested" in await event_types(sessions_store, session)


async def test_cancelling_the_same_turn_twice_changes_nothing_the_second_time(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await open_turn(sessions_store, OWNER, session, SPOKE)

    once = await cancel_turn(sessions_store, OWNER, str(turn["id"]))
    twice = await cancel_turn(sessions_store, OWNER, str(turn["id"]))

    assert once == twice
    assert (await event_types(sessions_store, session)).count("lucy.turn.cancelled") == 1


async def test_cancelling_a_queued_turn_behind_a_running_one_leaves_the_session_working(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    running = await a_running_turn(sessions_store, session)
    queued = await open_turn(sessions_store, OWNER, session, SPOKE)

    await cancel_turn(sessions_store, OWNER, str(queued["id"]))

    assert await session_status(sessions_store, session) == "running"
    assert (await sessions_store.turn(OWNER, running))["status"] == "running"


async def test_cancelling_a_turn_that_already_finished_never_reaches_its_successor(
    sessions_store: SessionStore,
) -> None:
    # The race a stale stop button is: the turn it was aimed at ended, another started, and
    # the click must land on neither.
    session = await a_session(sessions_store)
    finished = await open_turn(sessions_store, OWNER, session, SPOKE)
    await close_turn(
        sessions_store, OWNER, str(finished["id"]), Outcome("completed", "success", "end_turn")
    )
    successor = await a_running_turn(sessions_store, session)

    answered = await cancel_turn(sessions_store, OWNER, str(finished["id"]))

    assert answered["status"] == "completed"
    assert answered["cancel_requested"] == 0
    after = await sessions_store.turn(OWNER, successor)
    assert after["status"] == "running"
    assert after["cancel_requested"] == 0


async def test_cancelling_a_turn_belonging_to_somebody_else_finds_nothing(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    turn = await a_running_turn(sessions_store, session)

    with pytest.raises(LucyError) as caught:
        await cancel_turn(sessions_store, STRANGER, turn)

    assert caught.value.code == "not-found"
    assert (await sessions_store.turn(OWNER, turn))["cancel_requested"] == 0


async def test_cancelling_a_turn_that_never_existed_finds_nothing(
    sessions_store: SessionStore,
) -> None:
    with pytest.raises(LucyError) as caught:
        await cancel_turn(sessions_store, OWNER, "trn_invented")

    assert caught.value.code == "not-found"


async def test_a_page_of_turns_reads_in_the_order_they_were_opened(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    opened = [str((await open_turn(sessions_store, OWNER, session, SPOKE))["id"]) for _ in range(3)]

    first = await list_turns(sessions_store, OWNER, session, Cursor(limit=2))
    rest = await list_turns(
        sessions_store, OWNER, session, Cursor(limit=2, after=str(first["last_id"]))
    )

    assert [row["id"] for row in first["data"]] == opened[:2]
    assert first["has_more"] is True
    assert [row["id"] for row in rest["data"]] == opened[2:]
    assert rest["has_more"] is False


async def test_the_turns_of_another_accounts_session_are_not_listable(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await open_turn(sessions_store, OWNER, session, SPOKE)

    with pytest.raises(LucyError) as caught:
        await list_turns(sessions_store, STRANGER, session, Cursor())

    assert caught.value.code == "not-found"


async def test_the_live_turn_query_covers_every_state_that_is_not_terminal() -> None:
    """The SQL is built from the terminal set, so a new terminal state cannot be missed.

    Read off the statement rather than the constant, because the failure being guarded
    against is a hand-written list of three placeholders left behind when a fourth terminal
    state arrives -- at which point the query silently stops matching.
    """
    from lucy_api.sessions.models import TERMINAL
    from lucy_api.sessions.turns import _LIVE_TURNS

    assert _LIVE_TURNS.count("?") == len(TERMINAL) + 1
    sqlite3.complete_statement(_LIVE_TURNS + ";")
