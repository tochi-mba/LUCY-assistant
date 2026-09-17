"""Why the whole database sits behind one thread, demonstrated rather than asserted.

SQLite hands us serialisable writes for free only when exactly one connection is in play
and exactly one thread touches it. `SqlWorker` buys that by owning both, and in doing so
takes on three obligations that nothing above it can fix. A call has to carry its
exception back to the awaiting coroutine instead of dying quietly on a pool thread. Two
overlapping calls have to run one after the other, because the store's `BEGIN IMMEDIATE`
blocks assume no second writer exists. And shutdown has to close the connection on the
worker's own thread, since SQLite refuses to be closed from anywhere else and a connection
left open holds a write-ahead log the next process would have to recover.

The pragmas get their own test for a duller reason: `foreign_keys=ON` is what makes the
store's cascade deletes delete anything at all, and a pragma that quietly stopped being
applied would look like a session-store bug for a long time before anyone looked here.

Opening is deferred to the pool, which means a path that cannot be opened is not a
constructor error but a stored one, waiting for whoever calls first. That is the right
place for it -- a misconfigured directory should fail a request, not the import -- but it
only holds if the stored failure is actually re-raised, so it is tested rather than assumed.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from typing import TYPE_CHECKING

import pytest

from lucy_api.store.worker import SqlWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def _current_thread(connection: sqlite3.Connection) -> threading.Thread:
    return threading.current_thread()


def _journal_mode(connection: sqlite3.Connection) -> str:
    mode: str = connection.execute("PRAGMA journal_mode").fetchone()[0]
    return mode


def _names(directory: Path) -> list[str]:
    """Reading a directory is blocking, so an async test asks a thread to do it."""
    return sorted(entry.name for entry in directory.iterdir())


@pytest.fixture
async def worker(tmp_path: Path) -> AsyncIterator[SqlWorker]:
    """A file-backed worker, always closed: an open WAL keeps `tmp_path` undeletable."""
    open_worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    try:
        yield open_worker
    finally:
        await open_worker.aclose()


async def test_a_call_runs_on_the_workers_own_thread_and_hands_its_value_back(
    worker: SqlWorker,
) -> None:
    thread = await worker.call(_current_thread)

    assert thread is not threading.current_thread()
    assert thread.name.startswith("lucy-sqlite")
    assert await worker.call(lambda connection: connection.execute("SELECT 42").fetchone()[0]) == 42


async def test_the_connection_a_call_receives_arrives_already_configured(
    worker: SqlWorker,
) -> None:
    def pragmas(connection: sqlite3.Connection) -> tuple[int, int, int]:
        return (
            connection.execute("PRAGMA foreign_keys").fetchone()[0],
            connection.execute("PRAGMA busy_timeout").fetchone()[0],
            connection.execute("SELECT 1 AS answer").fetchone()["answer"],
        )

    # The third value only reads back by name if `row_factory` survived the handover, and
    # that is the whole reason the store is allowed to treat a row as a mapping.
    assert await worker.call(pragmas) == (1, 5000, 1)
    assert await worker.call(_journal_mode) == "wal"


async def test_every_call_sees_what_the_call_before_it_wrote(worker: SqlWorker) -> None:
    await worker.call(lambda connection: connection.execute("CREATE TABLE note (body TEXT)"))
    await worker.call(lambda connection: connection.execute("INSERT INTO note VALUES ('kept')"))

    rows = await worker.call(
        lambda connection: connection.execute("SELECT body FROM note").fetchall()
    )

    assert [row["body"] for row in rows] == ["kept"]


async def test_the_worker_opens_whatever_directories_the_database_path_implies(
    tmp_path: Path,
) -> None:
    """A fresh install names a file under a data directory nobody has created yet."""
    path = tmp_path / "state" / "hub" / "lucy.sqlite3"
    worker = SqlWorker(str(path))
    try:
        await worker.call(lambda connection: connection.execute("CREATE TABLE t (n INTEGER)"))
    finally:
        await worker.aclose()

    assert path.exists()


async def test_a_path_that_cannot_be_opened_is_reported_to_whoever_calls_first(
    tmp_path: Path,
) -> None:
    """A misconfigured data directory has to arrive as an error, not a hang and not silence."""
    worker = SqlWorker(str(tmp_path))  # A directory is a path, but it is not a database.
    try:
        with pytest.raises(sqlite3.OperationalError, match="unable to open database file"):
            await worker.call(lambda connection: connection.execute("SELECT 1"))
    finally:
        # `aclose` cannot be used here: it closes the connection through `call`, so it meets
        # the same stored failure and re-raises it before it ever reaches the shutdown,
        # leaving the pool thread alive. Stopping the pool directly is the honest cleanup
        # until `aclose` stops depending on an open connection to shut a closed one down.
        await asyncio.to_thread(worker._pool.shutdown, wait=True)


async def test_an_in_memory_database_belongs_to_exactly_one_worker() -> None:
    """`:memory:` is the one path that is not a path, so it skips the directory work."""
    first = SqlWorker(":memory:")
    second = SqlWorker(":memory:")
    try:
        assert await first.call(_journal_mode) == "memory"
        await first.call(lambda connection: connection.execute("CREATE TABLE private (n INTEGER)"))

        with pytest.raises(sqlite3.OperationalError, match="no such table: private"):
            await second.call(lambda connection: connection.execute("SELECT * FROM private"))
    finally:
        await first.aclose()
        await second.aclose()


async def test_an_exception_inside_a_call_reaches_the_coroutine_that_awaited_it(
    worker: SqlWorker,
) -> None:
    def refuse(connection: sqlite3.Connection) -> None:
        message = "the caller is the one who has to hear about this"
        raise LookupError(message)

    with pytest.raises(LookupError, match="has to hear about this"):
        await worker.call(refuse)

    with pytest.raises(sqlite3.OperationalError, match="no such table: absent"):
        await worker.call(lambda connection: connection.execute("SELECT * FROM absent"))

    # A failed call is not a failed worker: the thread and the connection are still there.
    assert await worker.call(lambda connection: connection.execute("SELECT 7").fetchone()[0]) == 7


async def test_two_overlapping_calls_run_one_after_the_other(worker: SqlWorker) -> None:
    """Serialisation is the property the store's `BEGIN IMMEDIATE` is allowed to assume."""
    log: list[str] = []

    def occupy(name: str) -> str:
        log.append("enter " + name)
        # Long enough that an interleaving worker would have to show up in `log`.
        time.sleep(0.05)
        log.append("leave " + name)
        return name

    finished = await asyncio.gather(
        worker.call(lambda connection: occupy("first")),
        worker.call(lambda connection: occupy("second")),
    )

    assert sorted(finished) == ["first", "second"]
    assert log[1] == "leave " + log[0].removeprefix("enter ")
    assert log[3] == "leave " + log[2].removeprefix("enter ")


async def test_closing_the_worker_ends_its_thread_and_leaves_no_journal_behind(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lucy.sqlite3"
    worker = SqlWorker(str(path))
    thread = await worker.call(_current_thread)
    await worker.call(lambda connection: connection.execute("CREATE TABLE t (n INTEGER)"))

    # While the connection is open, WAL means two sidecar files exist beside the database.
    assert await asyncio.to_thread(_names, tmp_path) == [
        "lucy.sqlite3",
        "lucy.sqlite3-shm",
        "lucy.sqlite3-wal",
    ]

    await worker.aclose()

    # A connection closed on its own thread checkpoints and removes them; one abandoned to
    # the garbage collector does not, and the next process has to recover the log.
    assert await asyncio.to_thread(_names, tmp_path) == ["lucy.sqlite3"]
    assert not thread.is_alive()


async def test_a_closed_worker_refuses_to_accept_further_work(tmp_path: Path) -> None:
    worker = SqlWorker(str(tmp_path / "lucy.sqlite3"))
    await worker.aclose()

    with pytest.raises(RuntimeError, match="after shutdown"):
        await worker.call(lambda connection: connection.execute("SELECT 1"))
