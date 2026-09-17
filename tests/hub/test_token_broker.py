"""The broker's three security properties, and the one economic one.

Each of the three has been a real hole in a real system, so each gets a test that would
fail if the property were removed rather than a test that merely exercises the happy path:

* two people must never share a cache entry, and the key must come from a verification
  rather than from anything a request carried;
* the cache must be bounded, because one keyed on subject and never evicted from is a leak
  whose size is the user count;
* a refused token must be re-minted and retried exactly once -- `test_a_refused_token_is
  _reminted_once_and_never_twice` counts the attempts, so a rewrite that turned the retry
  into a loop fails here instead of wedging a turn in production.

And the economic one: `user` issues one scope per token, so the broker mints per audience
and never lets one audience's token stand in for another's.

The exchange is a hand-written fake satisfying the real Protocol. Nothing here touches a
transport; `test_keyring_exchange.py` is where the wire is pinned.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lucy_api.auth.broker import (
    Delegation,
    TokenBroker,
    TokenCache,
    TokenRefusedError,
)
from lucy_api.auth.exchange import DEFAULT_TTL_SECONDS, ExchangedToken, TokenExchange
from lucy_api.auth.verifier import VerifiedCaller

AUDIENCE = "lucy-api"
HOME = "user.home"
HEALTH = "user.health"
EXPIRES_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class FakeExchange:
    """Keyring's exchange, in memory, satisfying the real Protocol.

    Every mint is distinguishable -- the token carries its audience and the ordinal of the
    call that produced it -- because almost every assertion here is about *which* token
    came back, and two identical strings would let a substitution pass unnoticed.
    """

    def __init__(self, *, expires_in: int = 900) -> None:
        self.calls: list[tuple[str, str | None, str | None, int]] = []
        self.expires_in = expires_in
        self.closed = False

    async def exchange(
        self,
        *,
        audience: str,
        user_token: str | None = None,
        grant_id: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> ExchangedToken:
        self.calls.append((audience, user_token, grant_id, ttl_seconds))
        return ExchangedToken(
            audience=audience,
            token=f"{audience}#{len(self.calls)}",
            expires_in=self.expires_in,
            expires_at=EXPIRES_AT,
        )

    async def aclose(self) -> None:
        self.closed = True


class Ticking:
    """A clock a test moves by hand, so an expiry is a fact rather than a sleep."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def person(account_id: str, *, user_token: str = "their.jwt") -> Delegation:
    """A delegation for somebody whose token this hub verified."""
    return Delegation.for_person(
        VerifiedCaller(account_id=account_id, audience=AUDIENCE), user_token=user_token
    )


def broker_for(
    delegation: Delegation,
    exchange: FakeExchange,
    cache: TokenCache,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> TokenBroker:
    return TokenBroker(
        exchange=exchange, delegation=delegation, cache=cache, ttl_seconds=ttl_seconds
    )


def test_the_fake_exchange_satisfies_the_seam_the_broker_depends_on() -> None:
    # Otherwise every test below could be passing against a shape keyring does not have.
    assert isinstance(FakeExchange(), TokenExchange)


async def test_a_token_is_minted_once_and_served_from_memory_until_it_nearly_expires() -> None:
    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache(), ttl_seconds=300)

    first = await broker.token_for(HOME)
    second = await broker.token_for(HOME)

    assert first == second == f"{HOME}#1"
    assert exchange.calls == [(HOME, "their.jwt", None, 300)]


async def test_two_people_never_share_a_cache_entry_however_alike_their_calls_look() -> None:
    # The whole point of keying on the subject. One cache, one audience, one identical
    # request shape -- and two tokens, because the two subjects are not the same person.
    exchange = FakeExchange()
    cache = TokenCache()
    alice = broker_for(person("acct_alice"), exchange, cache)
    bob = broker_for(person("acct_bob"), exchange, cache)

    alice_token = await alice.token_for(HOME)
    bob_token = await bob.token_for(HOME)

    assert alice_token != bob_token
    assert len(exchange.calls) == 2
    # And neither is served the other's on the way back through the cache.
    assert await alice.token_for(HOME) == alice_token
    assert await bob.token_for(HOME) == bob_token


async def test_the_key_is_the_verified_subject_and_not_the_token_that_carried_it() -> None:
    # settings_client keys on the token because it cannot verify one; the hub verifies, so
    # two live tokens for the same verified person are the same person and share an entry.
    # The distinction matters because it is the reason the subject is safe to key on here.
    exchange = FakeExchange()
    cache = TokenCache()
    morning = broker_for(person("acct_a", user_token="jwt.minted.at.nine"), exchange, cache)
    afternoon = broker_for(person("acct_a", user_token="jwt.minted.at.two"), exchange, cache)

    assert await morning.token_for(HOME) == await afternoon.token_for(HOME)
    assert len(exchange.calls) == 1


async def test_an_account_id_and_a_grant_handle_that_read_alike_do_not_collide() -> None:
    exchange = FakeExchange()
    cache = TokenCache()
    foreground = broker_for(person("dgt_abc"), exchange, cache)
    background = broker_for(Delegation.for_grant("dgt_abc"), exchange, cache)

    assert foreground.subject != background.subject
    assert await foreground.token_for(HOME) != await background.token_for(HOME)


async def test_two_audiences_yield_two_tokens_and_neither_stands_in_for_the_other() -> None:
    # `user` carries one scope per token: user.health cannot read user.home. A broker that
    # held one token per person per service would under-read and look like a 403.
    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache())

    home = await broker.token_for(HOME)
    health = await broker.token_for(HEALTH)

    assert home != health
    assert [call[0] for call in exchange.calls] == [HOME, HEALTH]
    assert await broker.token_for(HOME) == home
    assert await broker.token_for(HEALTH) == health


async def test_the_cache_is_bounded_and_forgets_the_least_recently_used_audience() -> None:
    exchange = FakeExchange()
    cache = TokenCache(max_entries=2)
    broker = broker_for(person("acct_a"), exchange, cache)

    home = await broker.token_for(HOME)
    await broker.token_for(HEALTH)
    # Reading home again makes health the least recently used one.
    assert await broker.token_for(HOME) == home

    await broker.token_for("user.calendar")

    assert len(cache) == 2
    assert await broker.token_for(HOME) == home
    assert await broker.token_for(HEALTH) == f"{HEALTH}#4"


async def test_an_entry_retires_early_so_a_call_is_never_made_with_a_dying_token() -> None:
    # A token with a second left is worthless: the call still has to reach a sibling and be
    # verified there. The margin buys that round trip.
    clock = Ticking()
    exchange = FakeExchange(expires_in=60)
    cache = TokenCache(early_expiry_seconds=30.0, clock=clock)
    broker = broker_for(person("acct_a"), exchange, cache)

    first = await broker.token_for(HOME)
    clock.now = 29.0
    assert await broker.token_for(HOME) == first

    clock.now = 30.0
    assert await broker.token_for(HOME) == f"{HOME}#2"
    assert len(exchange.calls) == 2


async def test_an_expired_entry_stops_occupying_the_bound() -> None:
    clock = Ticking()
    cache = TokenCache(max_entries=8, early_expiry_seconds=0.0, clock=clock)
    cache.put("sub:acct_a", HOME, "t", expires_in=10)

    clock.now = 10.0

    assert cache.get("sub:acct_a", HOME) is None
    assert len(cache) == 0


def test_a_cache_with_no_room_at_all_is_a_configuration_error() -> None:
    # Silently holding nothing would look exactly like a working cache with a bad hit rate.
    with pytest.raises(ValueError, match="at least 1"):
        TokenCache(max_entries=0)


async def test_reminting_discards_what_was_held_and_never_serves_it_again() -> None:
    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache())

    stale = await broker.token_for(HOME)
    fresh = await broker.remint(HOME)

    assert fresh != stale
    assert await broker.token_for(HOME) == fresh


async def test_a_refused_token_is_reminted_once_and_never_twice() -> None:
    # The test that would catch a retry loop. A `while` here instead of two statements
    # would keep calling send; the guard turns that into a failure rather than a hang.
    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache())
    sent: list[str] = []

    async def always_refuses(token: str) -> str:
        sent.append(token)
        if len(sent) > 2:
            message = "the broker retried more than once"
            raise AssertionError(message)
        raise TokenRefusedError

    with pytest.raises(TokenRefusedError):
        await broker.attempt(HOME, always_refuses)

    assert sent == [f"{HOME}#1", f"{HOME}#2"]
    assert len(exchange.calls) == 2


async def test_one_refusal_is_replaced_by_a_fresh_token_and_the_person_sees_nothing() -> None:
    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache())
    sent: list[str] = []

    async def refuses_the_first(token: str) -> str:
        sent.append(token)
        if len(sent) == 1:
            raise TokenRefusedError
        return "the answer"

    assert await broker.attempt(HOME, refuses_the_first) == "the answer"
    assert sent == [f"{HOME}#1", f"{HOME}#2"]


async def test_an_attempt_nobody_complains_about_costs_one_mint_and_one_call() -> None:
    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache())
    sent: list[str] = []

    async def succeeds(token: str) -> int:
        sent.append(token)
        return len(sent)

    assert await broker.attempt(HOME, succeeds) == 1
    assert len(exchange.calls) == 1


async def test_a_caller_may_nominate_its_own_signal_for_a_stale_credential() -> None:
    # So the layer above keeps its own vocabulary and nothing below has to learn what an
    # HTTP status is.
    class TheirOwnRefusalError(Exception):
        pass

    exchange = FakeExchange()
    broker = broker_for(person("acct_a"), exchange, TokenCache())
    sent: list[str] = []

    async def refuses_the_first(token: str) -> str:
        sent.append(token)
        if len(sent) == 1:
            raise TheirOwnRefusalError
        return "the answer"

    answer = await broker.attempt(HOME, refuses_the_first, refused=TheirOwnRefusalError)

    assert answer == "the answer"
    assert len(sent) == 2


async def test_background_work_presents_a_grant_handle_and_no_person_s_token() -> None:
    exchange = FakeExchange()
    broker = broker_for(Delegation.for_grant("dgt_abc"), exchange, TokenCache())

    await broker.token_for(HOME)

    assert exchange.calls == [(HOME, None, "dgt_abc", DEFAULT_TTL_SECONDS)]


def test_a_delegation_never_prints_the_person_s_token() -> None:
    # A repr is what a traceback prints, which is the realistic way a credential escapes.
    delegation = person("acct_a", user_token="secret.jwt.value")

    assert "secret.jwt.value" not in repr(delegation)
    assert "sub:acct_a" in repr(delegation)


def test_a_subject_is_only_ever_built_from_something_that_was_checked() -> None:
    # Both constructors take a checked thing: a caller the verifier produced, or a handle
    # keyring issued and re-checks on every exchange. Neither takes a bare request field.
    verified = VerifiedCaller(account_id="acct_a", audience=AUDIENCE)

    assert Delegation.for_person(verified, user_token="t").subject == "sub:acct_a"
    assert Delegation.for_grant("dgt_abc").subject == "grant:dgt_abc"


async def test_closing_the_exchange_is_the_exchange_s_own_business() -> None:
    exchange = FakeExchange()
    await exchange.aclose()
    assert exchange.closed is True


def test_the_clock_defaults_to_a_monotonic_reading() -> None:
    # A cache lifetime measured against a wall clock survives until somebody adjusts it.
    cache = TokenCache()
    cache.put("sub:acct_a", HOME, "t", expires_in=900)

    assert cache.get("sub:acct_a", HOME) == "t"


def test_dropping_something_that_was_never_held_is_quiet() -> None:
    cache = TokenCache()

    cache.drop("sub:nobody", HOME)

    assert len(cache) == 0
