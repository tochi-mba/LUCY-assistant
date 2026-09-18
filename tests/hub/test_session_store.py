"""The store's security property is one predicate, so it is tested at every door.

`SessionStore` is the only thing standing between two accounts' conversations, and it
defends them with a single idea: every externally addressable lookup carries the account
into the WHERE clause, and a row that does not match is `absent()` rather than "forbidden".
An identifier is unguessable, so leaking the difference between "not yours" and "not
there" would be the only way to confirm a session exists. That is why the stranger tests
below assert 404 and never 403, and why they enumerate every entry point instead of
sampling one: the predicate is written out again in each method, and a copied defence is
exactly the kind that grows a hole when a method is added. The enumeration is read back off
the class rather than trusted, because a hand-written list of doors is itself a copy, and it
would go quietly out of date on the day the hole appeared.

The rest follows the store's two other promises. A transcript is append-only, so items are
checked for a sequence that only climbs and a parent chain that never forks, and an item
write is checked to emit the event that tells a live listener it happened. And a retry is
not a second request, so the idempotency tests care about all three outcomes -- replay,
conflict, and the expiry that lets a key be honestly reused a long time later -- since
getting the middle one wrong is how a caller's network hiccup silently charges twice.

Every test drives a real SQLite file through the real worker. Faking the database here
would test the fake: half of what is being claimed is transactional behaviour, and the
other half is SQL.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import time
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import pytest

from lucy_api import __version__
from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.sql_store import (
    IdempotentWrite,
    NewItem,
    SessionStore,
    digest,
    encoded,
    identifier,
    page,
    row_value,
)
from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

OWNER = "acct_owner"
STRANGER = "acct_stranger"

GUARDED_ELSEWHERE = frozenset({"create", "list_sessions", "audit_log"})
"""Account-taking methods a stranger cannot meet a 404 at, and why.

`create` makes a row rather than finding one, so there is nothing to be refused access to.
A listing is a collection the caller owns, so an account with no sessions is honestly empty
rather than missing. The audit log is the same idea: it is this account's own rows, not a
lookup of somebody else's. Each is covered by its own tests; every other method is a door.
"""


class WriteFailedError(Exception):
    """Raised by a test operation that wants to watch its own transaction roll back."""


class InterruptedWriteError(BaseException):
    """A failure that is not an `Exception`, which is the case a narrower catch would miss."""


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    """A real database on disk; closing it matters because an open WAL pins `tmp_path`."""
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    opened = SessionStore(worker)
    await opened.initialize()
    try:
        yield opened
    finally:
        await worker.aclose()


async def a_session(store: SessionStore, account: str = OWNER, **fields: Any) -> str:
    """A created session's id. The key is fresh each time, so creation is never a replay."""
    created = await store.create(account, CreateSession(**fields), identifier("key"))
    return str(created["id"])


async def a_turn(store: SessionStore, session: str, status: str = "running") -> str:
    """A turn row. Nothing in the store creates one yet, so a test has to write it itself."""
    turn = identifier("trn")

    def apply(db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO turns (id,session_id,status,input_json,created_at) VALUES (?,?,?,?,?)",
            (turn, session, status, encoded({"events": []}), time.time()),
        )

    await store.transaction(apply)
    return turn


async def event_types(store: SessionStore, session: str, account: str = OWNER) -> list[str]:
    return [str(event["type"]) for event in await store.records(account, session, "events")]


def assert_not_found(caught: pytest.ExceptionInfo[LucyError], where: str) -> None:
    """A foreign row and a missing row have to be indistinguishable, code and status alike."""
    assert caught.value.code == "not-found", where
    assert caught.value.status == HTTPStatus.NOT_FOUND, where
    assert "not found" in str(caught.value), where


async def test_creating_the_schema_a_second_time_leaves_the_existing_rows_alone() -> None:
    """Startup runs the migration unconditionally, so it has to be safe on a warm database."""
    worker = SqlWorker(":memory:")
    store = SessionStore(worker)
    try:
        await store.initialize()
        session = await a_session(store)

        await store.initialize()

        assert (await store.get(OWNER, session))["id"] == session
    finally:
        await worker.aclose()


async def test_a_created_session_starts_idle_and_is_stamped_with_the_running_harness(
    store: SessionStore,
) -> None:
    created = await store.create(
        OWNER,
        CreateSession(title="Planning", model="openai:gpt-5", incognito=True),
        "create-once",
    )

    assert created["account_id"] == OWNER
    assert created["title"] == "Planning"
    assert created["status"] == "idle"
    assert created["durability_mode"] == "durable"
    assert created["harness_version"] == __version__
    assert created["id"].startswith("ses_")
    # SQLite has no boolean, so the flag comes back as the integer it was stored as, and
    # `== 1` alone would be just as true of a `True` that a future decoder invented.
    assert created["incognito"] == 1
    assert not isinstance(created["incognito"], bool)
    assert await event_types(store, str(created["id"])) == ["lucy.session.created"]


async def test_no_lookup_in_the_store_answers_for_an_account_that_does_not_own_the_row(
    store: SessionStore,
) -> None:
    """The one test that would notice a new method arriving without the account predicate."""
    session = await a_session(store)
    item = await store.append(OWNER, session, NewItem(kind="message", role="user", content="hi"))
    turn = await a_turn(store, session)

    doors: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        ("get", lambda: store.get(STRANGER, session)),
        (
            "attach_workspace",
            lambda: store.attach_workspace(STRANGER, session, "env-stolen", "sessions/stolen"),
        ),
        ("update", lambda: store.update(STRANGER, session, {"title": "taken over"})),
        ("append", lambda: store.append(STRANGER, session, NewItem("message", "user", "hi"))),
        ("event", lambda: store.event(STRANGER, session, "lucy.session.updated", None)),
        ("records", lambda: store.records(STRANGER, session, "items")),
        ("item", lambda: store.item(STRANGER, str(item["id"]))),
        ("turn", lambda: store.turn(STRANGER, turn)),
        ("finish_turn", lambda: store.finish_turn(STRANGER, turn, "cancelled")),
        ("delete", lambda: store.delete(STRANGER, session)),
        (
            "record_audit",
            lambda: store.record_audit(STRANGER, "permission.granted", session=session),
        ),
        (
            "record_steps",
            lambda: store.record_steps(STRANGER, session, turn, {"steps": []}, {"steps": []}),
        ),
        ("steps", lambda: store.steps(STRANGER, session, turn)),
    ]

    # The list above is hand-written, so on its own it would not notice a method that
    # arrived after it. Anything taking an `account` is a door until it is named otherwise.
    takes_an_account = {
        name
        for name, method in inspect.getmembers(SessionStore, inspect.isfunction)
        if not name.startswith("_") and "account" in inspect.signature(method).parameters
    }
    assert takes_an_account == {where for where, _ in doors} | GUARDED_ELSEWHERE

    for where, knock in doors:
        with pytest.raises(LucyError) as caught:
            await knock()
        assert_not_found(caught, where)

    # Nothing the stranger tried left a mark on the owner's session.
    assert (await store.get(OWNER, session))["title"] == "New conversation"
    assert (await store.turn(OWNER, turn))["status"] == "running"
    assert await event_types(store, session) == [
        "lucy.session.created",
        "lucy.content.item.added",
    ]


async def test_the_audit_log_is_this_account_s_rows_and_a_foreign_session_is_a_miss(
    store: SessionStore,
) -> None:
    """An audit row names whose fact it is; attaching it to somebody else's session is a 404."""
    session = await a_session(store)
    turn = await a_turn(store, session)

    await store.record_audit(
        OWNER,
        "permission.granted",
        session=session,
        turn=turn,
        detail={"permission": "notes.write"},
    )
    await store.record_audit(OWNER, "permission.revoked")

    with pytest.raises(LucyError) as caught:
        await store.record_audit(STRANGER, "permission.granted", session=session)
    assert_not_found(caught, "record_audit session")

    with pytest.raises(LucyError) as caught:
        await store.record_audit(STRANGER, "permission.granted", turn=turn)
    assert_not_found(caught, "record_audit turn")

    rows = await store.audit_log(OWNER)
    assert [row["action"] for row in rows] == ["permission.granted", "permission.revoked"]
    assert rows[0]["session_id"] == session
    assert rows[1]["session_id"] is None
    assert await store.audit_log(STRANGER) == []


async def test_a_session_that_never_existed_fails_exactly_as_a_foreign_one_does(
    store: SessionStore,
) -> None:
    with pytest.raises(LucyError) as caught:
        await store.get(OWNER, "ses_invented")
    assert_not_found(caught, "get")

    with pytest.raises(LucyError) as caught:
        await store.item(OWNER, "itm_invented")
    assert_not_found(caught, "item")

    with pytest.raises(LucyError) as caught:
        await store.turn(OWNER, "trn_invented")
    assert_not_found(caught, "turn")


async def test_an_update_writes_only_the_fields_that_were_actually_supplied(
    store: SessionStore,
) -> None:
    session = await a_session(store, title="Original", permission_mode="ask")

    renamed = await store.update(OWNER, session, {"title": "Renamed", "archived": None})

    assert renamed["title"] == "Renamed"
    assert renamed["permission_mode"] == "ask"
    assert renamed["input_policy"] == "enqueue"
    assert renamed["archived_at"] is None

    everything = await store.update(
        OWNER,
        session,
        {"title": "Final", "input_policy": "reject", "permission_mode": "plan"},
    )

    assert everything["title"] == "Final"
    assert everything["input_policy"] == "reject"
    assert everything["permission_mode"] == "plan"
    assert everything["updated_at"] >= renamed["updated_at"]


async def test_an_update_that_changes_nothing_still_says_that_it_happened(
    store: SessionStore,
) -> None:
    """A listener watching the event stream should not have to guess at an empty patch."""
    session = await a_session(store)

    unchanged = await store.update(OWNER, session, {})

    assert unchanged["title"] == "New conversation"
    assert await event_types(store, session) == ["lucy.session.created", "lucy.session.updated"]


async def test_archiving_and_unarchiving_move_a_timestamp_rather_than_a_flag(
    store: SessionStore,
) -> None:
    session = await a_session(store)

    archived = await store.update(OWNER, session, {"archived": True})
    assert archived["archived_at"] is not None

    restored = await store.update(OWNER, session, {"archived": False})
    assert restored["archived_at"] is None


async def test_a_session_left_by_an_older_harness_announces_the_change_when_it_is_touched(
    store: SessionStore,
) -> None:
    """An upgraded hub reopening yesterday's session is the moment a replay could diverge."""
    session = await a_session(store)

    def age(db: sqlite3.Connection) -> None:
        db.execute("UPDATE sessions SET harness_version=? WHERE id=?", ("0.0.1", session))

    await store.transaction(age)

    await store.update(OWNER, session, {"title": "Resumed"})

    events = await store.records(OWNER, session, "events")
    announced = [event for event in events if event["type"].endswith("harness_version_changed")]
    assert [event["data"] for event in announced] == [{"previous": "0.0.1", "current": __version__}]
    # The announcement precedes the update it explains, so a reader meets the cause first.
    assert await event_types(store, session) == [
        "lucy.session.created",
        "lucy.session.harness_version_changed",
        "lucy.session.updated",
    ]


async def test_a_session_updated_by_the_harness_that_wrote_it_announces_no_version_change(
    store: SessionStore,
) -> None:
    session = await a_session(store)

    await store.update(OWNER, session, {"title": "Still today"})

    assert "lucy.session.harness_version_changed" not in await event_types(store, session)


async def test_deleting_a_session_leaves_an_audit_row_and_takes_the_transcript_with_it(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    await store.append(OWNER, session, NewItem(kind="message", role="user", content="first"))
    await store.append(OWNER, session, NewItem(kind="message", role="assistant", content="second"))
    await a_turn(store, session)

    await store.delete(OWNER, session)

    with pytest.raises(LucyError) as caught:
        await store.get(OWNER, session)
    assert_not_found(caught, "get after delete")

    def survivors(db: sqlite3.Connection) -> tuple[int, int, int, list[tuple[str, str, str]]]:
        return (
            db.execute("SELECT COUNT(*) FROM items WHERE session_id=?", (session,)).fetchone()[0],
            db.execute("SELECT COUNT(*) FROM events WHERE session_id=?", (session,)).fetchone()[0],
            db.execute("SELECT COUNT(*) FROM turns WHERE session_id=?", (session,)).fetchone()[0],
            [
                (row["account_id"], row["session_id"], row["action"])
                for row in db.execute("SELECT * FROM audit ORDER BY sequence").fetchall()
            ],
        )

    # Every child table cascades away, but the fact of the deletion outlives the session --
    # and an audit row that could not say whose session it was would not be worth keeping.
    assert await store.worker.call(survivors) == (0, 0, 0, [(OWNER, session, "session.deleted")])


async def test_each_appended_item_takes_the_next_number_and_names_its_predecessor(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_turn(store, session)

    first = await store.append(OWNER, session, NewItem("message", "user", {"text": "hello"}))
    second = await store.append(
        OWNER, session, NewItem("message", "assistant", {"text": "hi"}, turn=turn, tokens=12)
    )

    assert (first["seq"], first["parent_id"]) == (1, None)
    assert (second["seq"], second["parent_id"]) == (2, first["id"])
    assert second["turn_id"] == turn
    assert second["tokens"] == 12
    # The stored content is JSON again on the way out, not a string that looks like JSON.
    assert (await store.item(OWNER, str(second["id"])))["content"] == {"text": "hi"}


async def test_appending_an_item_also_announces_the_item_on_the_event_stream(
    store: SessionStore,
) -> None:
    """A listener that had to poll the item table would lose the ordering guarantee."""
    session = await a_session(store)
    turn = await a_turn(store, session)

    item = await store.append(
        OWNER, session, NewItem("message", "user", {"text": "watch this"}, turn=turn)
    )

    events = await store.records(OWNER, session, "events")
    added = next(event for event in events if event["type"] == "lucy.content.item.added")
    assert added["turn_id"] == turn
    assert added["data"]["id"] == item["id"]
    assert added["data"]["content"] == {"text": "watch this"}


async def test_event_numbers_climb_within_a_session_and_start_over_in_the_next_one(
    store: SessionStore,
) -> None:
    """The number is a per-session cursor, so a client can resume one stream at a time."""
    first = await a_session(store)
    second = await a_session(store)

    await store.event(OWNER, first, "lucy.session.updated", {"n": 1})
    await store.event(OWNER, first, "lucy.session.updated", {"n": 2})
    recorded = await store.event(OWNER, second, "lucy.session.updated", {"n": 3})

    numbers = [event["sequence_number"] for event in await store.records(OWNER, first, "events")]
    assert numbers == [1, 2, 3]
    # The claim is about the second stream, so it is read off the second stream.
    assert [e["sequence_number"] for e in await store.records(OWNER, second, "events")] == [1, 2]
    assert recorded["sequence_number"] == 2
    assert recorded["turn_id"] is None
    assert recorded["data"] == {"n": 3}


async def test_an_event_can_be_attributed_to_the_turn_that_produced_it(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_turn(store, session)

    recorded = await store.event(OWNER, session, "lucy.turn.progress", {"step": 1}, turn)

    assert recorded["turn_id"] == turn
    assert recorded["session_id"] == session


async def test_records_refuses_a_table_that_is_not_part_of_a_session_transcript(
    store: SessionStore,
) -> None:
    """A table name cannot be a bound parameter, so the allowlist is all there is."""
    session = await a_session(store)

    with pytest.raises(ValueError, match="not a session record table"):
        await store.records(OWNER, session, "sessions")

    with pytest.raises(ValueError, match="not a session record table"):
        await store.records(OWNER, session, "items; DROP TABLE sessions")


async def test_records_returns_every_allowlisted_table_in_its_own_order(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_turn(store, session)
    await store.append(OWNER, session, NewItem("message", "user", "one"))
    await store.append(OWNER, session, NewItem("message", "assistant", "two"))

    def add_compactions(db: sqlite3.Connection) -> None:
        # Written out of order on purpose: the reader's order comes from `seq`, not rowid.
        for seq in (2, 1):
            db.execute(
                "INSERT INTO compactions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (identifier("cmp"), session, seq, 100, "m", "v1", "s", 1, seq, 1, time.time()),
            )

    await store.transaction(add_compactions)

    assert [item["seq"] for item in await store.records(OWNER, session, "items")] == [1, 2]
    assert [e["sequence_number"] for e in await store.records(OWNER, session, "events")] == [
        1,
        2,
        3,
    ]
    assert [row["id"] for row in await store.records(OWNER, session, "turns")] == [turn]
    assert [row["seq"] for row in await store.records(OWNER, session, "compactions")] == [1, 2]
    # A turn's stored input is decoded on the way out, exactly as an item's content is.
    assert (await store.records(OWNER, session, "turns"))[0]["input"] == {"events": []}


async def test_replaying_an_idempotency_key_with_the_same_body_returns_the_first_answer(
    store: SessionStore,
) -> None:
    attempts: list[int] = []

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        attempts.append(len(attempts) + 1)
        return {"attempt": len(attempts)}

    write = IdempotentWrite(account=OWNER, endpoint="/things", key="retried")

    first = await store.idempotent(write, apply)
    second = await store.idempotent(write, apply)

    assert first == second == {"attempt": 1}
    assert attempts == [1]


async def test_creating_a_session_twice_with_one_key_yields_one_session(
    store: SessionStore,
) -> None:
    """The retry a flaky network produces must not cost the person a second conversation."""
    request = CreateSession(title="Only once")

    created = await store.create(OWNER, request, "same-key")
    replayed = await store.create(OWNER, request, "same-key")

    assert replayed == created
    listed = await store.list_sessions(OWNER, 10, None, None, "asc")
    assert [session["id"] for session in listed["data"]] == [created["id"]]


async def test_one_idempotency_key_with_a_different_body_is_a_conflict(
    store: SessionStore,
) -> None:
    """Replaying the old answer would be worse than refusing: the caller asked for more."""
    await store.create(OWNER, CreateSession(title="First intent"), "reused")

    with pytest.raises(LucyError) as caught:
        await store.create(OWNER, CreateSession(title="Second intent"), "reused")

    assert caught.value.code == "conflict"
    assert caught.value.status == HTTPStatus.CONFLICT
    assert "already used for different input" in str(caught.value)
    # The refusal is not a half-write: the second session was never created.
    listed = await store.list_sessions(OWNER, 10, None, None, "asc")
    assert [session["title"] for session in listed["data"]] == ["First intent"]


async def test_one_idempotency_key_means_something_different_to_each_account(
    store: SessionStore,
) -> None:
    """Keys are chosen by callers, so two accounts picking the same one must not collide."""
    mine = await store.create(OWNER, CreateSession(title="Mine"), "shared-key")
    theirs = await store.create(STRANGER, CreateSession(title="Theirs"), "shared-key")

    assert mine["id"] != theirs["id"]
    assert (await store.get(STRANGER, str(theirs["id"])))["title"] == "Theirs"


async def test_an_expired_idempotency_record_is_swept_and_stops_causing_conflicts(
    store: SessionStore,
) -> None:
    """A key is only reserved for an hour; after that a caller may honestly reuse it."""
    attempts: list[int] = []

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        attempts.append(len(attempts) + 1)
        return {"attempt": len(attempts)}

    write = IdempotentWrite(account=OWNER, endpoint="/things", key="ages-ago", body={"n": 1})
    await store.idempotent(write, apply)

    def expire(db: sqlite3.Connection) -> None:
        db.execute("UPDATE idempotency SET expires_at=?", (time.time() - 1,))

    await store.transaction(expire)

    # A different body conflicts with a live record and is accepted when there is none. A
    # constant answer here would read the same whether the work re-ran or was replayed, so
    # the operation counts its own calls and the second answer has to be a second call.
    assert await store.idempotent(replace(write, body={"n": 2}), apply) == {"attempt": 2}
    assert attempts == [1, 2]

    def reserved(db: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = db.execute("SELECT key,request_hash FROM idempotency").fetchall()
        return [(row["key"], row["request_hash"]) for row in rows]

    # One record, and it is the new one: a sweep that only looked like it ran would leave
    # the old hash sitting here, and conflict with the next honest reuse of the key.
    assert await store.worker.call(reserved) == [("ages-ago", digest({"n": 2}))]


@pytest.mark.parametrize("failure", [WriteFailedError, InterruptedWriteError])
async def test_a_failing_transaction_leaves_the_database_as_it_found_it(
    store: SessionStore, failure: type[BaseException]
) -> None:
    """Rollback is what lets the idempotency record and the work it describes share a commit.

    An `Exception` and a bare `BaseException` are both tried because the hub owns exactly one
    connection. A transaction abandoned open by a narrower catch would corrupt nothing; it
    would simply refuse every write that came after it, for as long as the process lived.
    """
    session = await a_session(store, title="Intact")

    def apply(db: sqlite3.Connection) -> None:
        db.execute("UPDATE sessions SET title=? WHERE id=?", ("half written", session))
        raise failure

    with pytest.raises(failure):
        await store.transaction(apply)

    assert (await store.get(OWNER, session))["title"] == "Intact"
    # The rollback released the connection, so the next writer gets its own BEGIN IMMEDIATE.
    assert (await store.update(OWNER, session, {"title": "After"}))["title"] == "After"


async def test_an_idempotent_write_that_fails_records_neither_the_work_nor_the_key(
    store: SessionStore,
) -> None:
    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        db.execute(
            "INSERT INTO audit(account_id,action,detail_json,at) VALUES (?,?,?,?)",
            (OWNER, "never.happened", "{}", time.time()),
        )
        raise WriteFailedError

    write = IdempotentWrite(account=OWNER, endpoint="/things", key="doomed")
    with pytest.raises(WriteFailedError):
        await store.idempotent(write, apply)

    def leftovers(db: sqlite3.Connection) -> tuple[int, int]:
        return (
            db.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            db.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0],
        )

    assert await store.worker.call(leftovers) == (0, 0)


async def test_finishing_a_turn_stamps_it_and_hands_the_session_back_to_the_person(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_turn(store, session)

    await store.finish_turn(OWNER, turn, "completed", "end_turn", "stop")

    finished = await store.turn(OWNER, turn)
    assert finished["status"] == "completed"
    assert finished["termination"] == "end_turn"
    assert finished["stop_reason"] == "stop"
    assert finished["finished_at"] is not None
    assert (await store.get(OWNER, session))["status"] == "idle"
    assert await event_types(store, session) == ["lucy.session.created", "lucy.turn.completed"]


async def test_a_turn_that_has_already_ended_ignores_a_second_verdict(
    store: SessionStore,
) -> None:
    """Terminal is terminal: a cancellation arriving late must not rewrite a completed turn."""
    session = await a_session(store)
    turn = await a_turn(store, session)
    await store.finish_turn(OWNER, turn, "completed", "end_turn", "stop")
    settled = await store.turn(OWNER, turn)

    await store.finish_turn(OWNER, turn, "cancelled", "by_user", "abort")

    assert await store.turn(OWNER, turn) == settled
    assert "lucy.turn.cancelled" not in await event_types(store, session)


async def test_a_turn_that_vanishes_while_it_is_being_finished_is_not_brought_back(
    store: SessionStore,
) -> None:
    """`finish_turn` proves ownership in one transaction and writes its verdict in another.

    Between those two the row can be gone -- deleted, or cascaded away with its session --
    and the verdict has to land on nothing rather than resurrect a turn nobody can reach.
    The worker runs one call at a time in the order the calls arrive, so queueing the delete
    while the first half is in flight puts it exactly in that gap rather than racing for it.
    """
    session = await a_session(store)
    turn = await a_turn(store, session)

    def remove(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM turns WHERE id=?", (turn,))

    await asyncio.gather(
        store.finish_turn(OWNER, turn, "completed", "end_turn", "stop"),
        store.transaction(remove),
    )

    with pytest.raises(LucyError) as caught:
        await store.turn(OWNER, turn)
    assert_not_found(caught, "turn deleted mid-finish")
    # The session outlives its turn and is still answerable, which is the point of not
    # letting a write land on a row the caller was no longer holding.
    assert (await store.get(OWNER, session))["id"] == session


async def test_a_record_whose_json_column_is_empty_reads_back_as_nothing(
    store: SessionStore,
) -> None:
    """A step has no result until it has run, so `result_json` is null for its whole life.

    Every stored blob the store writes today is non-null, so the decoder's empty case is
    reached only by a column the schema already allows to be empty. A decoder that assumed
    otherwise would not fail here; it would fail the first time a step was read mid-flight.
    """
    session = await a_session(store)
    turn = await a_turn(store, session)

    def unfinished(db: sqlite3.Connection) -> dict[str, Any]:
        db.execute(
            "INSERT INTO steps VALUES (?,?,?,?,?,?,?,?)",
            (session, turn, "stp_running", "tool", "running", digest({}), None, time.time()),
        )
        row = db.execute("SELECT * FROM steps WHERE step_id=?", ("stp_running",)).fetchone()
        return row_value(row)

    decoded = await store.transaction(unfinished)

    assert decoded["result"] is None
    # The suffix is stripped either way, so a reader never has to know a column was empty.
    assert "result_json" not in decoded


async def test_a_cursor_naming_another_accounts_session_is_refused_not_ignored(
    store: SessionStore,
) -> None:
    """A cursor is an identifier, and honouring an unknown one answers "does this exist?".

    Starting from the top instead would be worse than a refusal in both directions: the
    caller gets a page they did not ask for, and a stranger gets to tell a real identifier
    from an invented one by whether the listing moved.
    """
    mine = await a_session(store)
    theirs = await a_session(store, account=STRANGER)

    with pytest.raises(LucyError) as caught:
        await store.list_sessions(OWNER, 10, theirs, None, "asc")
    assert_not_found(caught, "after")

    with pytest.raises(LucyError) as caught:
        await store.list_sessions(OWNER, 10, None, theirs, "asc")
    assert_not_found(caught, "before")

    listed = await store.list_sessions(OWNER, 10, None, None, "asc")
    assert [session["id"] for session in listed["data"]] == [mine]


async def test_a_non_terminal_status_leaves_the_turn_unfinished_and_the_session_busy(
    store: SessionStore,
) -> None:
    """A turn waiting on the person is still that person's turn, not a closed one."""
    session = await a_session(store)
    turn = await a_turn(store, session)

    await store.finish_turn(OWNER, turn, "input_required")

    waiting = await store.turn(OWNER, turn)
    assert waiting["status"] == "input_required"
    assert waiting["finished_at"] is None
    assert waiting["termination"] is None
    assert (await store.get(OWNER, session))["status"] == "input_required"

    # Not being terminal, it can still be resolved afterwards.
    await store.finish_turn(OWNER, turn, "failed", "error", "tool_error")
    assert (await store.turn(OWNER, turn))["finished_at"] is not None


async def test_listing_sessions_never_reaches_across_accounts(store: SessionStore) -> None:
    mine = {await a_session(store), await a_session(store)}
    await a_session(store, account=STRANGER)

    listed = await store.list_sessions(OWNER, 10, None, None, "asc")

    assert {session["id"] for session in listed["data"]} == mine
    assert listed["has_more"] is False
    assert len((await store.list_sessions(STRANGER, 10, None, None, "asc"))["data"]) == 1


async def test_a_listing_reads_backwards_and_resumes_from_a_cursor(
    store: SessionStore,
) -> None:
    """`created_at` ties on a coarse clock, so the ascending page is the order of record."""
    for _ in range(4):
        await a_session(store)
    first_page = await store.list_sessions(OWNER, 10, None, None, "asc")
    ascending = [session["id"] for session in first_page["data"]]

    descending = await store.list_sessions(OWNER, 10, None, None, "desc")
    assert [session["id"] for session in descending["data"]] == list(reversed(ascending))

    after = await store.list_sessions(OWNER, 10, ascending[1], None, "asc")
    assert [session["id"] for session in after["data"]] == ascending[2:]

    before = await store.list_sessions(OWNER, 10, None, ascending[2], "asc")
    assert [session["id"] for session in before["data"]] == ascending[:2]

    capped = await store.list_sessions(OWNER, 2, None, None, "asc")
    assert capped["has_more"] is True
    assert (capped["first_id"], capped["last_id"]) == (ascending[0], ascending[1])

    with pytest.raises(LucyError) as caught:
        await store.list_sessions(OWNER, 10, "ses_never_issued", None, "asc")
    assert_not_found(caught, "list_sessions")


async def test_two_appends_to_one_session_cannot_claim_the_same_sequence_number(
    store: SessionStore,
) -> None:
    """The worker's single thread is what makes the read-then-write inside `item_row` safe."""
    session = await a_session(store)

    written = await asyncio.gather(
        *(store.append(OWNER, session, NewItem("message", "user", {"n": n})) for n in range(5))
    )

    assert sorted(item["seq"] for item in written) == [1, 2, 3, 4, 5]
    chain = await store.records(OWNER, session, "items")
    assert [item["parent_id"] for item in chain] == [None, *[item["id"] for item in chain[:-1]]]


def test_a_page_reads_forwards_or_backwards_over_the_same_rows() -> None:
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

    ascending = page(rows, 10, None, None, "asc")
    descending = page(rows, 10, None, None, "desc")

    assert [row["id"] for row in ascending["data"]] == ["a", "b", "c"]
    assert (ascending["first_id"], ascending["last_id"]) == ("a", "c")
    assert [row["id"] for row in descending["data"]] == ["c", "b", "a"]
    assert (descending["first_id"], descending["last_id"]) == ("c", "a")


def test_a_page_says_there_is_more_only_when_it_held_something_back() -> None:
    """`has_more` decides whether a caller walks on, so an off-by-one truncates in silence.

    The flag is read beside the length it describes. Alone it would still look right for a
    page that returned every row and denied holding any back, which is the same lie told
    from the other end.
    """
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

    held = page(rows, 2, None, None, "asc")
    exact = page(rows, 3, None, None, "asc")
    spare = page(rows, 4, None, None, "asc")

    assert (len(held["data"]), held["has_more"]) == (2, True)
    assert (len(exact["data"]), exact["has_more"]) == (3, False)
    assert (len(spare["data"]), spare["has_more"]) == (3, False)


def test_a_page_bounded_at_both_ends_returns_only_what_lies_between() -> None:
    rows = [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]

    between = page(rows, 10, "a", "d", "asc")

    assert [row["id"] for row in between["data"]] == ["b", "c"]
    # Read backwards, the same pair of identifiers bounds the window from the other side.
    assert [row["id"] for row in page(rows, 10, "d", "a", "desc")["data"]] == ["c", "b"]


def test_an_empty_page_has_no_first_or_last_identifier_to_offer() -> None:
    empty = page([], 10, None, None, "asc")

    assert empty == {"data": [], "has_more": False, "first_id": None, "last_id": None}


def test_a_cursor_from_outside_the_collection_is_refused_rather_than_ignored() -> None:
    """Silently starting from the top would hand the caller a page they did not ask for."""
    rows = [{"id": "a"}, {"id": "b"}]

    with pytest.raises(LucyError) as after:
        page(rows, 10, "elsewhere", None, "asc")
    assert after.value.status == HTTPStatus.NOT_FOUND

    with pytest.raises(LucyError) as before:
        page(rows, 10, None, "elsewhere", "asc")
    assert before.value.status == HTTPStatus.NOT_FOUND


async def test_a_restart_fails_turns_left_running_and_leaves_queued_ones_runnable(
    store: SessionStore,
) -> None:
    """A crashed process cannot prove a tool did not already run, so it does not rerun it."""
    session = await a_session(store)
    abandoned = await a_turn(store, session, status="running")
    waiting = await a_turn(store, session, status="queued")

    interrupted = await store.interrupt_abandoned_turns()

    assert interrupted == (abandoned,)
    failed = await store.turn(OWNER, abandoned)
    assert failed["status"] == "failed"
    assert failed["stop_reason"] == "process_restarted"
    assert failed["error_code"] == "process_restarted"
    assert (await store.turn(OWNER, waiting))["status"] == "queued"
    assert (await store.get(OWNER, session))["status"] == "queued"
    items = await store.records(OWNER, session, "items")
    assert items[-1]["type"] == "error"
    assert items[-1]["content"]["code"] == "process_restarted"
    assert "lucy.turn.failed" in await event_types(store, session)


async def test_a_turn_waiting_on_a_person_survives_a_restart(store: SessionStore) -> None:
    session = await a_session(store)
    parked = await a_turn(store, session, status="input_required")

    assert await store.interrupt_abandoned_turns() == ()
    assert (await store.turn(OWNER, parked))["status"] == "input_required"


async def test_interrupting_nothing_is_a_no_op(store: SessionStore) -> None:
    session = await a_session(store)

    assert await store.interrupt_abandoned_turns() == ()
    assert (await store.get(OWNER, session))["status"] == "idle"


async def test_a_session_with_only_the_abandoned_turn_goes_idle(store: SessionStore) -> None:
    session = await a_session(store)
    abandoned = await a_turn(store, session, status="running")

    assert await store.interrupt_abandoned_turns() == (abandoned,)
    assert (await store.get(OWNER, session))["status"] == "idle"


async def test_a_parked_turn_keeps_the_session_waiting_after_a_restart(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    abandoned = await a_turn(store, session, status="running")
    parked = await a_turn(store, session, status="input_required")

    interrupted = await store.interrupt_abandoned_turns()

    assert interrupted == (abandoned,)
    assert (await store.turn(OWNER, parked))["status"] == "input_required"
    assert (await store.get(OWNER, session))["status"] == "input_required"


async def test_executed_steps_are_recorded_once_and_junk_is_skipped(
    store: SessionStore,
) -> None:
    session = await a_session(store)
    turn = await a_turn(store, session)
    plan = {"steps": [{"id": "search", "op": "notes.search", "input": {"q": "tea"}}]}
    result = {
        "steps": [
            "skip",
            {"id": ""},
            {"id": "search", "status": "ok", "operation": "notes.search"},
        ]
    }

    await store.record_steps(OWNER, session, turn, plan, result)
    await store.record_steps(
        OWNER,
        session,
        turn,
        plan,
        {"steps": [{"id": "search", "status": "ok", "op": "notes.search"}]},
    )
    rows = await store.steps(OWNER, session, turn)

    assert len(rows) == 1
    assert rows[0]["step_id"] == "search"
    assert rows[0]["kind"] == "notes.search"
    assert rows[0]["status"] == "ok"
    assert rows[0]["input_digest"]
