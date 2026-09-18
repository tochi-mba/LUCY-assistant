"""Short-lived probe answers and the lock that stops a refresh looking like replay.

A probe is a network round-trip to somebody else's service. Running it at the top of every
turn, for every pack, is how a conversation that never mentions music still waits on
music's devices list. Caching the *availability* — not the bound operations — for a few
seconds is the cheap answer: operations still close over this turn's context, and a connect
or a 502 naming a missing credential drops the row so the next turn sees the truth.

Refresh tokens are a different problem. Two concurrent calls that both cause a sibling to
refresh the same stored grant are indistinguishable from replay, and RFC 9700 tells the
authorization server to revoke the chain. Lucy never holds the refresh token, but it is
the one that can send two calls at once, so the lock lives here, per (person, profile,
audience).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from lucy_api.clients.errors import BAD_GATEWAY, CREDENTIAL_CODES, problem_code

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.packs.base import Availability
    from lucy_api.packs.context import Call, Http

PROBE_TTL_SECONDS = 15.0
"""Long enough to skip a burst of probes in one conversation, short enough that a missed
invalidation is a stale turn rather than a stale minute."""


class ProbeCache:
    """Availability keyed by (account, profile, pack), with a monotonic clock."""

    def __init__(
        self,
        *,
        ttl_seconds: float = PROBE_TTL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now or time.monotonic
        self._rows: dict[tuple[str, str, str], tuple[float, Availability]] = {}

    def get(self, account_id: str, profile: str, pack_id: str) -> Availability | None:
        key = (account_id, profile, pack_id)
        row = self._rows.get(key)
        if row is None:
            return None
        expires_at, availability = row
        if self._now() >= expires_at:
            self._rows.pop(key, None)
            return None
        return availability

    def put(self, account_id: str, profile: str, pack_id: str, availability: Availability) -> None:
        self._rows[(account_id, profile, pack_id)] = (self._now() + self._ttl, availability)

    def drop(self, account_id: str, profile: str, pack_id: str | None = None) -> None:
        if pack_id is not None:
            self._rows.pop((account_id, profile, pack_id), None)
            return
        stale = [key for key in self._rows if key[0] == account_id and key[1] == profile]
        for key in stale:
            del self._rows[key]


class ProviderLocks:
    """One exclusive lock per (account, profile, audience)."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def lock_for(self, account_id: str, profile: str, audience: str) -> asyncio.Lock:
        return self._locks.setdefault((account_id, profile, audience), asyncio.Lock())


class GuardedHttp:
    """An ``Http`` that serialises one person's calls to one audience and notices disconnects.

    The inner client's ``request`` must not call this object's ``request_response``: the
    lock is not re-entrant, and a nested acquire on the same audience would deadlock a turn.
    PackHttp is safe — its ``request`` calls its own ``request_response``.
    """

    def __init__(
        self,
        inner: Http,
        locks: ProviderLocks,
        *,
        account_id: str,
        profile: str,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.inner = inner
        self.locks = locks
        self._account_id = account_id
        self._profile = profile
        self._on_disconnect = on_disconnect

    async def request(self, call: Call) -> Any:
        async with self.locks.lock_for(self._account_id, self._profile, call.audience):
            return await self.inner.request(call)

    async def request_response(self, call: Call) -> Any:
        async with self.locks.lock_for(self._account_id, self._profile, call.audience):
            response = await self.inner.request_response(call)
            self._drop_if_disconnected(response)
            return response

    def _drop_if_disconnected(self, response: Any) -> None:
        if self._on_disconnect is None or getattr(response, "status_code", 0) != BAD_GATEWAY:
            return
        try:
            body = response.json()
        except (TypeError, ValueError, AttributeError):
            return
        if problem_code(body) in CREDENTIAL_CODES:
            self._on_disconnect()


__all__ = [
    "PROBE_TTL_SECONDS",
    "GuardedHttp",
    "ProbeCache",
    "ProviderLocks",
]
