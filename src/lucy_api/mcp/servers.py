"""Per-account registry of external MCP servers, hash-pinned at import.

A row is one server a person registered. Its tools never enter the system prompt; the
digest is how Lucy notices a listing that changed under them.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError, absent, conflict
from lucy_api.mcp.pin import pin
from lucy_api.net.ssrf import assert_public_https

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from lucy_api.mcp.outbound import Listing
    from lucy_api.sessions.sql_store import SessionStore

NAME_TAKEN = "An MCP server with that name is already registered."
PIN_MISMATCH = "pin_mismatch"
READY = "ready"
UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class McpServer:
    id: str
    account_id: str
    name: str
    url: str
    state: str
    tools: tuple[dict[str, Any], ...]
    digest: str
    last_seen: float | None
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "state": self.state,
            "tools": list(self.tools),
            "digest": self.digest,
            "last_seen": self.last_seen,
            "created_at": self.created_at,
        }


RESERVED = frozenset(
    {
        "help",
        "music",
        "research",
        "notes",
        "workspace",
        "settings",
        "work",
        "agents",
        "capabilities",
        "lucy",
        "mcp",
    }
)
RESERVED_CODE = "mcp-reserved-name"
RESERVED_NAME = "That name is already a Lucy capability."


class McpServers:
    def __init__(self, store: SessionStore, listing: Listing, *, clock: Any = time.time) -> None:
        self._store = store
        self.listing = listing
        self._clock = clock
        self._listed: dict[str, tuple[dict[str, Any], ...]] = {}

    async def list(self, account: str) -> list[dict[str, Any]]:
        rows = await self._store.worker.call(lambda db: _select_all(db, account))
        payload = tuple(row.as_dict() for row in rows)
        self._listed[account] = payload
        return list(payload)

    def cached(self, account: str) -> tuple[dict[str, Any], ...]:
        """Last listing for this account, populated by :meth:`list` during probe."""
        return self._listed.get(account, ())

    async def get(self, account: str, name: str) -> dict[str, Any]:
        row = await self._store.worker.call(lambda db: _select_one(db, account, name))
        if row is None:
            raise absent()
        return row.as_dict()

    async def register(self, account: str, name: str, url: str) -> dict[str, Any]:
        if name in RESERVED:
            raise LucyError(RESERVED_CODE, RESERVED_NAME, 409)
        checked = assert_public_https(url)
        tools = await self.listing(checked)
        pinned = pin(tools)
        now = float(self._clock())
        identifier = secrets.token_urlsafe(16)

        def write(db: sqlite3.Connection) -> McpServer:
            if _select_one(db, account, name) is not None:
                raise conflict(NAME_TAKEN)
            db.execute(
                """
                INSERT INTO mcp_servers (
                    id, account_id, name, url, credential_service, state,
                    tools_json, tools_digest, last_seen, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    account,
                    name,
                    checked,
                    READY,
                    pinned.payload,
                    pinned.digest,
                    now,
                    now,
                ),
            )
            return _require(_select_one(db, account, name))

        stored = await self._store.transaction(write)
        return stored.as_dict()

    async def refresh(self, account: str, name: str) -> dict[str, Any]:
        current = await self.get(account, name)
        checked = assert_public_https(str(current["url"]))
        try:
            tools = await self.listing(checked)
        except LucyError:
            return await self._set_state(account, name, UNREACHABLE)
        pinned = pin(tools)
        if pinned.digest != current["digest"]:
            return await self._set_state(account, name, PIN_MISMATCH)
        now = float(self._clock())

        def write(db: sqlite3.Connection) -> McpServer:
            db.execute(
                """
                UPDATE mcp_servers
                SET state = ?, last_seen = ?
                WHERE account_id = ? AND name = ?
                """,
                (READY, now, account, name),
            )
            return _require(_select_one(db, account, name))

        return (await self._store.transaction(write)).as_dict()

    async def delete(self, account: str, name: str) -> None:
        def write(db: sqlite3.Connection) -> None:
            cursor = db.execute(
                "DELETE FROM mcp_servers WHERE account_id = ? AND name = ?",
                (account, name),
            )
            if cursor.rowcount == 0:
                raise absent()

        await self._store.transaction(write)

    async def _set_state(self, account: str, name: str, state: str) -> dict[str, Any]:
        def write(db: sqlite3.Connection) -> McpServer:
            db.execute(
                "UPDATE mcp_servers SET state = ? WHERE account_id = ? AND name = ?",
                (state, account, name),
            )
            return _require(_select_one(db, account, name))

        return (await self._store.transaction(write)).as_dict()


def _select_all(db: sqlite3.Connection, account: str) -> list[McpServer]:
    rows = db.execute(
        """
        SELECT id, account_id, name, url, state, tools_json, tools_digest, last_seen, created_at
        FROM mcp_servers WHERE account_id = ? ORDER BY name
        """,
        (account,),
    ).fetchall()
    return [_from_row(row) for row in rows]


def _select_one(db: sqlite3.Connection, account: str, name: str) -> McpServer | None:
    row = db.execute(
        """
        SELECT id, account_id, name, url, state, tools_json, tools_digest, last_seen, created_at
        FROM mcp_servers WHERE account_id = ? AND name = ?
        """,
        (account, name),
    ).fetchone()
    if row is None:
        return None
    return _from_row(row)


def _require(row: McpServer | None) -> McpServer:
    if row is None:
        raise absent()
    return row


def _from_row(row: Mapping[str, Any]) -> McpServer:
    tools = json.loads(row["tools_json"])
    if not isinstance(tools, list):
        tools = []
    return McpServer(
        id=str(row["id"]),
        account_id=str(row["account_id"]),
        name=str(row["name"]),
        url=str(row["url"]),
        state=str(row["state"]),
        tools=tuple(item for item in tools if isinstance(item, dict)),
        digest=str(row["tools_digest"]),
        last_seen=row["last_seen"],
        created_at=float(row["created_at"]),
    )
