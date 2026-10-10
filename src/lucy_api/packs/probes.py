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

A turn takes an answer for fifteen seconds. The window gauge takes one for an hour: it counts
what a turn would send, and an answer that old counts it as well as a new one, where asking
again costs a keyring exchange and a sibling's reply for every capability.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from lucy_api.clients.errors import BAD_GATEWAY, CREDENTIAL_CODES, problem_code

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from lucy_api.packs.base import Availability
    from lucy_api.packs.context import Call, Http

PROBE_TTL_SECONDS = 15.0
"""Long enough to skip a burst of probes in one conversation, short enough that a missed
invalidation is a stale turn rather than a stale minute."""

KNOWN_SECONDS = 3_600.0
"""How old an answer the window gauge still takes, and how long one is kept at all.

`GET /context/window` took 6.1 s on its first read and 88 ms after. Its figure includes the
capability list and the plan schema a turn would send, so it probed every capability, and
past the turn's fifteen seconds every probe was a network round again: a keyring exchange per
sibling, which keyring answers one at a time, then each sibling's reply, the slowest a search
service waking from idle -- all for a token count an answer from earlier gives as well. An
hour, so a conversation a person comes back to still has one; no longer, so an answer that
went wrong with nobody saying so does not stand all day. Connecting, disconnecting and a
step's missing credential still drop answers the moment they happen.
"""

_probing: ContextVar[bool] = ContextVar("lucy_probing", default=False)


@contextmanager
def probing() -> Iterator[None]:
    """Mark the calls made inside as a probe's own. See `GuardedHttp`."""
    token = _probing.set(True)
    try:
        yield
    finally:
        _probing.reset(token)


class ProbeCache:
    """Availability keyed by (account, profile, session, pack), with a monotonic clock.

    An answer is fresh for `ttl_seconds`, which is what a turn takes. A reader that only
    estimates passes `within` to take an older one; past `KNOWN_SECONDS` nobody does, and the
    answer is forgotten.

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
        """Each answer, with the moment it was found."""

    def get(
        self,
        account_id: str,
        profile: str,
        pack_id: str,
        *,
        session_id: str = "",
        within: float | None = None,
    ) -> Availability | None:
        """The answer found for this pack, if it is younger than `within` seconds.

        `within` is the TTL unless a reader says otherwise: see `KNOWN_SECONDS`.
        """
        key = (account_id, profile, session_id, pack_id)
        row = self._rows.get(key)
        if row is None:
            return None
        found_at, availability = row
        age = self._now() - found_at
        if age >= max(self._ttl, KNOWN_SECONDS):
            del self._rows[key]
            return None
        if age >= (self._ttl if within is None else within):
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
        self._rows[key] = (self._now(), availability)

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

    Not when a probe gets it. A probe that finds the credential missing has found out what it
    was asked, and its answer is cached as its own row. Dropping everybody else's with it, as
    this did, emptied the cache on every probe round of anybody with one capability not
    connected: every capability that had answered first was asked again next time.
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
        if _probing.get():
            return
        try:
            body = response.json()
        except (TypeError, ValueError, AttributeError):
            return
        if problem_code(body) in CREDENTIAL_CODES:
            self._on_disconnect()


__all__ = [
    "KNOWN_SECONDS",
    "PROBE_TTL_SECONDS",
    "GuardedHttp",
    "ProbeCache",
    "probing",
]
