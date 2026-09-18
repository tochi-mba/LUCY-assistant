"""Store webhook URLs and fire one-line signals when a turn changes state."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from lucy_api.core.errors import LucyError, absent
from lucy_api.net.ssrf import assert_public_https
from lucy_api.sessions.sql_store import encoded, identifier, row_value

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore

logger = logging.getLogger(__name__)

Deliver = Callable[[str, dict[str, str], bytes], Awaitable[None]]
SIGNALS = frozenset({"completed", "failed", "cancelled", "input_required", "auth_required"})
TOO_MANY = "This account already has as many webhooks as Lucy will store."
MAX_HOOKS = 20
"""A handful of destinations is a notifier. A hundred is a fan-out attack."""


class Webhooks:
    """Account-scoped destinations. A stranger's id is a miss, never a 403."""

    def __init__(self, store: SessionStore, *, deliver: Deliver | None = None) -> None:
        self._store = store
        self.deliver = deliver

    async def register(self, account: str, url: str) -> dict[str, Any]:
        """Record one HTTPS destination. The signing secret is in this response only."""
        checked = assert_public_https(url)

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            existing = db.execute(
                "SELECT * FROM webhooks WHERE account_id=? AND url=?", (account, checked)
            ).fetchone()
            if existing is not None:
                return {
                    "id": existing["id"],
                    "url": existing["url"],
                    "created_at": existing["created_at"],
                }
            count = db.execute(
                "SELECT COUNT(*) AS n FROM webhooks WHERE account_id=?", (account,)
            ).fetchone()
            if int(count["n"]) >= MAX_HOOKS:
                code = "webhook-limit"
                raise LucyError(code, TOO_MANY, 409)
            hook_id = identifier("whk")
            secret = secrets.token_urlsafe(32)
            now = time.time()
            db.execute(
                "INSERT INTO webhooks(id,account_id,url,secret,created_at) VALUES (?,?,?,?,?)",
                (hook_id, account, checked, secret, now),
            )
            return {
                "id": hook_id,
                "url": checked,
                "secret": secret,
                "created_at": now,
            }

        return await self._store.transaction(apply)

    async def for_account(self, account: str) -> list[dict[str, Any]]:
        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = db.execute(
                "SELECT id,url,created_at FROM webhooks WHERE account_id=? ORDER BY created_at,id",
                (account,),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self._store.worker.call(read)

    async def delete(self, account: str, hook_id: str) -> None:
        def apply(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT id FROM webhooks WHERE id=? AND account_id=?", (hook_id, account)
            ).fetchone()
            if row is None:
                raise absent()
            db.execute("DELETE FROM webhooks WHERE id=? AND account_id=?", (hook_id, account))

        await self._store.transaction(apply)

    async def notify(self, account: str, session_id: str, turn_id: str, status: str) -> None:
        """Fan out a signal. Delivery failures are logged by type, never by body."""
        if status not in SIGNALS or self.deliver is None:
            return
        hooks = await self._with_secrets(account)
        payload = encoded({"session_id": session_id, "turn_id": turn_id, "status": status}).encode()
        for hook in hooks:
            signature = hmac.new(str(hook["secret"]).encode(), payload, hashlib.sha256).hexdigest()
            headers = {
                "content-type": "application/json",
                "x-lucy-signature": f"sha256={signature}",
            }
            try:
                await self.deliver(str(hook["url"]), headers, payload)
            except Exception as exc:
                logger.info(
                    "webhook_delivery_failed",
                    extra={"webhook_id": hook["id"], "error": type(exc).__name__},
                )

    async def _with_secrets(self, account: str) -> list[dict[str, Any]]:
        def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = db.execute(
                "SELECT id,url,secret FROM webhooks WHERE account_id=? ORDER BY created_at,id",
                (account,),
            ).fetchall()
            return [row_value(row) for row in rows]

        return await self._store.worker.call(read)


def httpx_deliver(client: httpx.AsyncClient) -> Deliver:
    """POST the signal. Failures are logged by exception type, never by body or URL."""

    async def post(url: str, headers: dict[str, str], body: bytes) -> None:
        try:
            await client.post(url, headers=headers, content=body)
        except httpx.HTTPError as exc:
            logger.info("webhook_delivery_failed", extra={"error": type(exc).__name__})

    return post


__all__ = ["MAX_HOOKS", "SIGNALS", "Webhooks", "httpx_deliver"]
