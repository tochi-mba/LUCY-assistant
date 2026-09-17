"""Minting the short-lived tokens Lucy acts with, and holding them for as briefly as works.

Three properties here are security requirements rather than performance choices, and each
of them is a place somebody has shipped a hole before.

**The cache is keyed on the verified subject.** ``settings_client`` keys its cache on the
token itself and its docstring explains why: it does not verify tokens, so an unverified
``sub`` is a string the caller supplied, and anything able to present a forged token
claiming ``sub: victim`` would be served the victim's cache entry without a request ever
reaching the server. The hub is in the opposite position -- it verifies locally, in
:mod:`lucy_api.auth.verifier` -- so keying on the subject is correct here and only here.
:class:`Delegation` is the guarantee that the distinction survives: the only way to build
one for a person is to hand it a :class:`~lucy_api.auth.verifier.VerifiedCaller`, which
only a completed verification produces. There is no path from a request body to a key.

**The cache is bounded.** A dictionary keyed on subject that is never evicted from is a
leak whose size is your user count, and it is invisible until the machine that has been up
longest falls over. The bound is an LRU, which for tokens is nearly free: the entries worth
keeping are the ones somebody is using.

**One scope per token.** ``user`` issues one audience per token, so ``user.health`` cannot
read ``user.home``. A broker that held one token per person per service would silently
under-read -- the calls would 403 in ways that look like a connection problem -- so the
audience is part of the key and one audience's token is never handed out for another.

A ``401`` from a sibling means *the token expired in flight*, which happens because tokens
are short-lived by design. :meth:`TokenBroker.attempt` re-mints and retries exactly once.
Once, and provably: the retry is straight-line code with no loop, so a sibling answering
401 forever costs two requests rather than a wedged turn. The second refusal is a real
failure and is allowed through, because a hub that retried forever to avoid a visible error
would replace one bad turn with an unbounded one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.auth.exchange import DEFAULT_TTL_SECONDS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from lucy_api.auth.exchange import TokenExchange
    from lucy_api.auth.verifier import VerifiedCaller
    from lucy_api.packs.context import TokenSource

DEFAULT_MAX_ENTRIES = 512
"""How many (subject, audience) pairs are held before the least recently used is dropped.

Sized for a hub, not for a leaf service: one person reaching four capabilities holds four
entries, so this is a few hundred people in flight at once. Raise it when the eviction rate
says to, and never remove it.
"""

DEFAULT_EARLY_EXPIRY_SECONDS = 30.0
"""How long before real expiry an entry stops being served.

A token that is valid for one more second is worthless: the call it is attached to has to
reach a sibling and be verified there. The margin buys the round trip, and it is the
difference between a cache hit and a 401 that a person waits through.
"""

SUBJECT_PREFIX = "sub:"
GRANT_PREFIX = "grant:"


class TokenRefusedError(Exception):
    """A sibling refused a minted token. The signal that turns one 401 into a re-mint.

    Raised by whatever :meth:`TokenBroker.attempt` is given to run, never by this module,
    and never seen by a person: it exists so the broker can tell "your credential is stale"
    apart from every other way a downstream call can fail, without the broker having to
    know what an HTTP response is.
    """


@dataclass(frozen=True, slots=True, repr=False)
class Delegation:
    """Who Lucy is acting for, and by what authority.

    ``subject`` is the cache key, and the two constructors are the only two places it is
    ever made. Both derive it from something that was checked somewhere: an account id off
    a token this hub verified itself, or a grant handle keyring issued and will check
    again on every exchange. The prefixes keep the two namespaces apart, because an account
    id and a grant id colliding would be a cross-person cache hit and nothing would say so.
    """

    subject: str
    user_token: str | None = None
    grant_id: str | None = None

    @classmethod
    def for_person(cls, caller: VerifiedCaller, *, user_token: str) -> Delegation:
        """Act for the person whose token this hub just verified, while they are here."""
        return cls(subject=SUBJECT_PREFIX + caller.account_id, user_token=user_token)

    @classmethod
    def for_grant(cls, grant_id: str) -> Delegation:
        """Act under standing consent, for work that outlives the person's session.

        The account is not named and is not Lucy's to name: keyring reads it off the grant,
        which is the same rule the foreground path follows and the reason neither path can
        be talked into acting for somebody else.
        """
        return cls(subject=GRANT_PREFIX + grant_id, grant_id=grant_id)

    def __repr__(self) -> str:
        """The user token is absent from this on purpose: a repr is what a traceback prints."""
        return f"Delegation(subject={self.subject!r}, grant_id={self.grant_id!r})"


@dataclass(slots=True, repr=False)
class _Entry:
    """One minted token and the moment it stops being worth serving.

    ``repr=False`` so the dataclass does not generate one that prints the token.
    """

    token: str
    not_after: float


class TokenCache:
    """Minted tokens, bounded and keyed on the verified subject.

    Process-scoped and shared by every broker, which is what makes the bound necessary and
    what makes the key matter. A broker is built per person per turn; this outlives both.

    Args:
        max_entries: the LRU bound. See :data:`DEFAULT_MAX_ENTRIES`.
        early_expiry_seconds: how early an entry is retired. See
            :data:`DEFAULT_EARLY_EXPIRY_SECONDS`.
        clock: elapsed seconds. Monotonic by default, because a cache lifetime has to
            survive the machine's wall clock being adjusted underneath it.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        early_expiry_seconds: float = DEFAULT_EARLY_EXPIRY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            message = f"max_entries must be at least 1; got {max_entries}"
            raise ValueError(message)
        self._max_entries = max_entries
        self._margin = early_expiry_seconds
        self._clock = clock
        self._entries: dict[tuple[str, str], _Entry] = {}

    def __len__(self) -> int:
        """How many tokens are held. The assertion in a test about the bound."""
        return len(self._entries)

    def get(self, subject: str, audience: str) -> str | None:
        """The live token for this subject and audience, or ``None`` to mint a new one."""
        key = (subject, audience)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._clock() >= entry.not_after:
            # Dropped rather than left to be overwritten, so a subject that stops being
            # used stops occupying the bound.
            del self._entries[key]
            return None
        # Re-inserted at the most-recently-used end. A plain dict relying on insertion
        # order, rather than OrderedDict, because this is the one operation OrderedDict
        # would give a name to.
        self._entries[key] = self._entries.pop(key)
        return entry.token

    def put(self, subject: str, audience: str, token: str, expires_in: int) -> None:
        """Hold a freshly minted token until just inside its expiry."""
        key = (subject, audience)
        self._entries.pop(key, None)
        self._entries[key] = _Entry(
            token=token, not_after=self._clock() + expires_in - self._margin
        )
        while len(self._entries) > self._max_entries:
            del self._entries[next(iter(self._entries))]

    def drop(self, subject: str, audience: str) -> None:
        """Forget one token, because something downstream has just refused it."""
        self._entries.pop((subject, audience), None)


class TokenBroker:
    """One person's authority, minted per audience and held for as briefly as works.

    Satisfies :class:`~lucy_api.packs.context.TokenSource`, which is what a capability is
    handed: a thing that will produce a credential for the length of one outbound call,
    rather than a credential sitting in a structure the formatter can walk into a label the
    model reads.

    Deliberately not single-flighted. Two concurrent misses for one audience cost one extra
    exchange, and the usual fix -- a table of locks keyed on subject -- is a second
    unbounded structure to get right, which is the thing :class:`TokenCache` exists to
    avoid. It belongs here the day a profile fans out to a dozen packs at once, and not
    before.

    Args:
        exchange: keyring's exchange.
        delegation: who this broker acts for. See :class:`Delegation`.
        cache: the shared, bounded store. Shared on purpose; see :class:`TokenCache`.
        ttl_seconds: the lifetime asked for. Keyring caps it at its own limit and at the
            delegation's expiry, so this is a request and never a promise.
    """

    def __init__(
        self,
        *,
        exchange: TokenExchange,
        delegation: Delegation,
        cache: TokenCache,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._exchange = exchange
        self._delegation = delegation
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    @property
    def subject(self) -> str:
        """The cache key this broker mints under. Useful to assert on, never to set."""
        return self._delegation.subject

    async def token_for(self, audience: str) -> str:
        """A live token for exactly this audience, from the cache or freshly minted.

        Raises:
            ExchangeError: keyring refused or could not be reached. The subclass names who
                has to act; see :mod:`lucy_api.auth.exchange`.
        """
        cached = self._cache.get(self._delegation.subject, audience)
        if cached is not None:
            return cached
        return await self._mint(audience)

    async def remint(self, audience: str) -> str:
        """Discard whatever is held for this audience and mint again.

        For the one case that is not a failure: a sibling refused a token that had not
        expired by this hub's reckoning, which means this hub's reckoning was wrong.
        """
        self._cache.drop(self._delegation.subject, audience)
        return await self._mint(audience)

    async def attempt[T](
        self,
        audience: str,
        send: Callable[[str], Awaitable[T]],
        *,
        refused: type[Exception] = TokenRefusedError,
    ) -> T:
        """Run ``send`` with a token for ``audience``, re-minting once if it is refused.

        The retry is written as two statements rather than a loop, which is the whole
        argument for this method existing: "exactly once" is then a property of the shape
        of the code and not of a counter somebody can get wrong later. The second call is
        deliberately outside the ``try``, so a sibling that refuses everything costs two
        requests and then tells the truth.

        ``refused`` is a parameter rather than a fixed import so that the layer above can
        keep its own vocabulary -- the packs' HTTP client signals with a private exception
        of its own -- and nothing below has to learn what an HTTP status is.

        Args:
            audience: the sibling this call is for.
            send: makes one attempt with the token it is given. Raises ``refused`` if that
                token was the thing rejected.
            refused: the exception ``send`` raises to mean "that credential was stale".
        """
        try:
            return await send(await self.token_for(audience))
        except refused:
            return await send(await self.remint(audience))

    async def _mint(self, audience: str) -> str:
        """One exchange, cached under this broker's subject and this audience alone."""
        minted = await self._exchange.exchange(
            audience=audience,
            user_token=self._delegation.user_token,
            grant_id=self._delegation.grant_id,
            ttl_seconds=self._ttl_seconds,
        )
        self._cache.put(self._delegation.subject, audience, minted.token, minted.expires_in)
        return minted.token


if TYPE_CHECKING:

    def _a_broker_is_a_token_source(broker: TokenBroker) -> TokenSource:
        """Checked by mypy and never run: this is what ``PackContext.tokens`` holds.

        A capability only ever sees the Protocol, so nothing but this states the
        relationship anywhere the type checker can read it.
        """
        return broker


__all__ = [
    "DEFAULT_EARLY_EXPIRY_SECONDS",
    "DEFAULT_MAX_ENTRIES",
    "Delegation",
    "TokenBroker",
    "TokenCache",
    "TokenRefusedError",
]
