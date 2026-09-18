"""Durable RFC 8628-style device authorization for command-line sign-in."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.core.errors import LucyError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from lucy_api.store.worker import SqlWorker

DEVICE_SECONDS = 600
POLL_SECONDS = 5
SLOW_DOWN_SECONDS = 5
PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"
CONSUMED = "consumed"
EXPIRED_TOKEN = "expired_token"  # noqa: S105 - RFC error code, not a credential
INVALID_REQUEST = "invalid_request"
SLOW_DOWN = "slow_down"
AUTHORIZATION_PENDING = "authorization_pending"
ACCESS_DENIED = "access_denied"
_USER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class DeviceFlowError(LucyError):
    """One OAuth device-flow error, retaining RFC 8628's exact error word."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code, detail, 400)


@dataclass(frozen=True, slots=True)
class DeviceCode:
    device_code: str
    user_code: str
    expires_at: float
    interval: int


@dataclass(frozen=True, slots=True)
class DeviceToken:
    access_token: str
    account_id: str


class DeviceFlow:
    """Issue, approve and redeem one-time codes on Lucy's SQLite writer."""

    def __init__(
        self,
        worker: SqlWorker,
        *,
        clock: Callable[[], float] = time.time,
        issue_device: Callable[[], str] | None = None,
        issue_user: Callable[[], str] | None = None,
    ) -> None:
        self._worker = worker
        self._clock = clock
        self._issue_device = issue_device or (lambda: secrets.token_urlsafe(32))
        self._issue_user = issue_user or _user_code

    async def create(self) -> DeviceCode:
        """Create an unapproved code without knowing who will approve it."""
        now = self._clock()
        record = DeviceCode(
            device_code=self._issue_device(),
            user_code=self._issue_user(),
            expires_at=now + DEVICE_SECONDS,
            interval=POLL_SECONDS,
        )

        def insert(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM device_codes WHERE expires_at<=?", (now,))
            db.execute(
                "INSERT INTO device_codes VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    record.device_code,
                    record.user_code,
                    None,
                    PENDING,
                    record.interval,
                    None,
                    now,
                    record.expires_at,
                    None,
                ),
            )

        await self._worker.call(insert)
        return record

    async def decide(
        self,
        user_code: str,
        *,
        account_id: str,
        access_token: str,
        approve: bool,
    ) -> None:
        """Bind the code to the authenticated browser subject, or deny it."""
        now = self._clock()

        def update(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT state,expires_at FROM device_codes WHERE user_code=?", (user_code,)
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                raise DeviceFlowError(EXPIRED_TOKEN, "The device code has expired.")
            if row["state"] != PENDING:
                raise DeviceFlowError(INVALID_REQUEST, "The device code was already decided.")
            db.execute(
                "UPDATE device_codes SET account_id=?,state=?,session_token=? WHERE user_code=?",
                (
                    account_id,
                    APPROVED if approve else DENIED,
                    access_token if approve else None,
                    user_code,
                ),
            )

        await self._worker.call(update)

    async def poll(self, device_code: str) -> DeviceToken:
        """Redeem an approved code once, enforcing the code's polling interval."""
        now = self._clock()

        def redeem(db: sqlite3.Connection) -> DeviceToken:
            row = db.execute(
                "SELECT * FROM device_codes WHERE device_code=?", (device_code,)
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                raise DeviceFlowError(EXPIRED_TOKEN, "The device code has expired.")
            interval = int(row["interval"])
            last_polled = row["last_polled_at"]
            if last_polled is not None and now - float(last_polled) < interval:
                db.execute(
                    "UPDATE device_codes SET interval=?,last_polled_at=? WHERE device_code=?",
                    (interval + SLOW_DOWN_SECONDS, now, device_code),
                )
                raise DeviceFlowError(SLOW_DOWN, "Poll less often.")
            db.execute(
                "UPDATE device_codes SET last_polled_at=? WHERE device_code=?", (now, device_code)
            )
            if row["state"] == PENDING:
                raise DeviceFlowError(AUTHORIZATION_PENDING, "Authorization is still pending.")
            if row["state"] == DENIED:
                raise DeviceFlowError(ACCESS_DENIED, "Authorization was denied.")
            if row["state"] != APPROVED or not row["session_token"] or not row["account_id"]:
                raise DeviceFlowError(EXPIRED_TOKEN, "The device code is no longer usable.")
            token = DeviceToken(str(row["session_token"]), str(row["account_id"]))
            db.execute(
                "UPDATE device_codes SET state=?,session_token=NULL WHERE device_code=?",
                (CONSUMED, device_code),
            )
            return token

        return await self._worker.call(redeem)


def _user_code() -> str:
    raw = "".join(secrets.choice(_USER_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


__all__ = ["DEVICE_SECONDS", "DeviceCode", "DeviceFlow", "DeviceFlowError", "DeviceToken"]
