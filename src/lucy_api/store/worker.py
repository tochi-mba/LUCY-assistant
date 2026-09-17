"""All SQLite work stays on one dedicated thread, including connection teardown."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


def _open(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


class SqlWorker:
    """Serialize transactions without blocking the asyncio event loop."""

    def __init__(self, path: str) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lucy-sqlite")
        self._connection = self._pool.submit(_open, path)

    async def call[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def execute() -> T:
            return operation(self._connection.result())

        return await asyncio.wrap_future(self._pool.submit(execute))

    async def aclose(self) -> None:
        await self.call(lambda connection: connection.close())
        await asyncio.to_thread(self._pool.shutdown, wait=True)
