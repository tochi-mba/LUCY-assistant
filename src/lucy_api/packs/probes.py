"""Short-lived probe answers, and noticing a sibling that has lost its credential.

A probe is a network round-trip to somebody else's service. Running it at the top of every
turn, for every pack, is how a conversation that never mentions music still waits on
music's devices list. Caching the *availability* — not the bound operations — for a few
seconds is the cheap answer: operations still close over this turn's context, and a connect
or a 502 naming a missing credential drops the row so the next turn sees the truth.

There was a lock here too, one per (person, profile, audience), so that two calls could
not make a sibling refresh one stored grant twice -- which RFC 9700 tells a provider to
treat as replay. It held every call for as long as the call took, and some calls take as
long as the work they ask for: a background command held the person's sandbox for its
whole run, so the next round's live block waited 25 seconds for `python count.py`, and of
two helpers searching at once the second timed out queueing behind the first's slow
summaries. No sibling holds a refresh token: every one asks keyring, and keyring renews a
grant once however many ask (Keyring-api, 2026-10-07). The lock guarded nothing.
"""

from __future__ import annotations

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
    """Availability keyed by (account, profile, session, pack), with a monotonic clock.

    The session is in the key because a probe can depend on it: the workspace is ready only
    in a session that has one attached. Keyed by person and pack alone, one probe made
    outside a session -- the capability listing, or a session not yet provisioned -- cached
    "no workspace is attached" for every session of that person for the whole TTL, and the
    workspace vanished from their tool lists. Dropping a row still reaches every session.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PROBE_TTL_SECONDS,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now or time.monotonic
        self._rows: dict[tuple[str, str, str, str], tuple[float, Availability]] = {}

    def get(
        self, account_id: str, profile: str, pack_id: str, *, session_id: str = ""
    ) -> Availability | None:
        key = (account_id, profile, session_id, pack_id)
        row = self._rows.get(key)
        if row is None:
            return None
        expires_at, availability = row
        if self._now() >= expires_at:
            self._rows.pop(key, None)
            return None
        return availability

    def put(
        self,
        account_id: str,
        profile: str,
        pack_id: str,
        availability: Availability,
        *,
        session_id: str = "",
    ) -> None:
        key = (account_id, profile, session_id, pack_id)
        self._rows[key] = (self._now() + self._ttl, availability)

    def drop(self, account_id: str, profile: str, pack_id: str | None = None) -> None:
        """Forget this person's answers for one pack, or for all of them, in every session."""
        stale = [
            key
            for key in self._rows
            if key[0] == account_id and key[1] == profile and (pack_id is None or key[3] == pack_id)
        ]
        for key in stale:
            del self._rows[key]


class GuardedHttp:
    """An ``Http`` that notices a sibling saying the person's credential is gone.

    A 502 naming a missing credential drops the person's cached probes, so the next turn
    sees the capability as it now is rather than as it was a few seconds ago.
    """

    def __init__(
        self,
        inner: Http,
        *,
        account_id: str,
        profile: str,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.inner = inner
        self._account_id = account_id
        self._profile = profile
        self._on_disconnect = on_disconnect

    async def request(self, call: Call) -> Any:
        return await self.inner.request(call)

    async def request_response(self, call: Call) -> Any:
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
]
