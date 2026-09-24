"""The one place a token is attached, and the one place it could be the wrong one.

The test this file exists for is `test_a_caller_s_token_never_reaches_a_sibling`. The hub's
whole delegation story rests on it: MCP's authorization spec forbids transiting a caller's
token, and the family's confused-deputy defence assumes nobody does. Everything else here
is the behaviour that makes the promise survivable in practice -- a short-lived token that
expires mid-turn is re-minted once, silently, so being strict about credentials does not
mean being unreliable about turns.

The broker is the real one, over a fake exchange, so what is exercised is the two of them
agreeing. The siblings are `httpx.MockTransport`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

from lucy_api.auth.broker import Delegation, TokenBroker, TokenCache
from lucy_api.auth.exchange import DEFAULT_TTL_SECONDS, ExchangedToken
from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.clients.errors import RateLimitedError
from lucy_api.clients.transport import Sibling
from lucy_api.packs.context import Call, PackContext
from lucy_api.packs.http import (
    MALFORMED,
    SUBJECT_HEADER,
    DownstreamRefusedError,
    DownstreamRejectedError,
    DownstreamUnavailableError,
    PackHttp,
    apply_downstream_policy,
)
from lucy_api.settings.policy import TurnPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

HOME = "user.home"
URL = "http://user.test/v1/home"
CALLER_TOKEN = "the.callers.own.aud-lucy-api.jwt"


class FakeExchange:
    """Keyring's exchange, in memory. Each mint is distinguishable from the last."""

    def __init__(self) -> None:
        self.mints = 0

    async def exchange(
        self,
        *,
        audience: str,
        user_token: str | None = None,
        grant_id: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> ExchangedToken:
        self.mints += 1
        return ExchangedToken(
            audience=audience,
            token=f"minted-{audience}-{self.mints}",
            expires_in=900,
            expires_at=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
        )

    async def aclose(self) -> None:
        """Nothing to release."""


def a_broker(exchange: FakeExchange) -> TokenBroker:
    return TokenBroker(
        exchange=exchange,
        delegation=Delegation.for_person(
            VerifiedCaller(account_id="acct_a", audience="lucy-api"), user_token=CALLER_TOKEN
        ),
        cache=TokenCache(),
    )


@pytest.fixture
async def make_client() -> AsyncIterator[Callable[..., PackHttp]]:
    """Build pack clients over a mock transport and close every one of them afterwards."""
    built: list[PackHttp] = []

    def factory(
        handler: Callable[[httpx.Request], httpx.Response],
        broker: TokenBroker,
        *,
        service_tokens: dict[str, str] | None = None,
    ) -> PackHttp:
        client = PackHttp(
            tokens=broker,
            transport=httpx.MockTransport(handler),
            service_tokens=service_tokens,
        )
        built.append(client)
        return client

    yield factory
    for client in built:
        await client.aclose()


def recording(
    *answers: Callable[[], httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """A handler that answers in order, repeating the last answer, and records every call."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answers[min(len(seen), len(answers)) - 1]()

    return handler, seen


def ok(**kwargs: object) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, **kwargs)


def refusing() -> httpx.Response:
    return httpx.Response(401, json={"detail": "token refused"})


async def test_every_outbound_call_carries_a_token_lucy_minted_for_that_audience(
    make_client,
) -> None:
    handler, seen = recording(ok(json={"rooms": 3}))
    exchange = FakeExchange()
    client = make_client(handler, a_broker(exchange))

    body = await client.request(Call(method="GET", url=URL, audience=HOME))

    assert body == {"rooms": 3}
    assert seen[0].headers["Authorization"] == f"Bearer minted-{HOME}-1"
    assert exchange.mints == 1


async def test_a_caller_s_token_never_reaches_a_sibling(make_client) -> None:
    # The hub holds the person's aud=lucy-api token for the length of a turn. A pack
    # written by copying an inbound request would forward it, and the sibling would then be
    # holding authority it was never meant to see. Nothing leaves with it on board.
    handler, seen = recording(ok(json={}))
    client = make_client(handler, a_broker(FakeExchange()))

    await client.request(
        Call(
            method="GET",
            url=URL,
            audience=HOME,
            headers={
                "Authorization": f"Bearer {CALLER_TOKEN}",
                "X-Keyring-User-Token": CALLER_TOKEN,
                "Cookie": f"session={CALLER_TOKEN}",
            },
        )
    )

    assert all(CALLER_TOKEN not in value for value in seen[0].headers.values())
    assert seen[0].headers["Authorization"] == f"Bearer minted-{HOME}-1"
    assert "cookie" not in seen[0].headers


SERVICE_SECRET = "lucy-own-service-token-not-a-person"


async def test_an_internal_audience_sends_two_credentials_and_never_the_caller(
    make_client,
) -> None:
    """Memory-api (and Keyring) want Lucy's token as Bearer and a minted person token beside it.

    The inbound caller JWT is still stripped: putting it in X-Keyring-User-Token would be
    the same confused-deputy the minted-Bearer path already refuses.
    """
    handler, seen = recording(ok(json={"ok": True}))
    client = make_client(handler, a_broker(FakeExchange()), service_tokens={HOME: SERVICE_SECRET})

    await client.request(
        Call(
            method="GET",
            url=URL,
            audience=HOME,
            headers={
                "Authorization": f"Bearer {CALLER_TOKEN}",
                SUBJECT_HEADER: CALLER_TOKEN,
            },
        )
    )

    assert seen[0].headers["Authorization"] == f"Bearer {SERVICE_SECRET}"
    assert seen[0].headers[SUBJECT_HEADER] == f"minted-{HOME}-1"
    assert all(CALLER_TOKEN not in value for value in seen[0].headers.values())
    assert SERVICE_SECRET not in repr(
        PackContext(
            account_id="acct_a",
            profile="personal",
            session_id="ses_1",
            http=client,
            tokens=a_broker(FakeExchange()),
        )
    )


async def test_an_expired_subject_token_is_reminted_without_changing_the_service_token(
    make_client,
) -> None:
    handler, seen = recording(refusing, ok(json={"ok": True}))
    exchange = FakeExchange()
    client = make_client(handler, a_broker(exchange), service_tokens={HOME: SERVICE_SECRET})

    assert await client.request(Call(method="GET", url=URL, audience=HOME)) == {"ok": True}
    assert [request.headers["Authorization"] for request in seen] == [
        f"Bearer {SERVICE_SECRET}",
        f"Bearer {SERVICE_SECRET}",
    ]
    assert seen[0].headers[SUBJECT_HEADER] == f"minted-{HOME}-1"
    assert seen[1].headers[SUBJECT_HEADER] == f"minted-{HOME}-2"
    assert exchange.mints == 2


async def test_a_blank_service_token_is_not_an_internal_call(make_client) -> None:
    handler, seen = recording(ok(json={}))
    client = make_client(handler, a_broker(FakeExchange()), service_tokens={HOME: "   "})

    await client.request(Call(method="GET", url=URL, audience=HOME))

    assert seen[0].headers["Authorization"] == f"Bearer minted-{HOME}-1"
    assert SUBJECT_HEADER not in seen[0].headers


async def test_sibling_adapter_uses_real_authenticated_transport(make_client) -> None:
    handler, seen = recording(refusing, ok(json={"rooms": 3}))
    exchange = FakeExchange()
    client = make_client(handler, a_broker(exchange))
    sibling = Sibling(client, "http://user.test", "user", HOME)

    assert await sibling.send("GET", "/v1/home") == {"rooms": 3}
    assert exchange.mints == 2
    assert len(seen) == 2


async def test_sibling_adapter_preserves_retry_after(make_client) -> None:
    handler, seen = recording(
        lambda: httpx.Response(429, json={"detail": "wait"}, headers={"Retry-After": "7"})
    )
    client = make_client(handler, a_broker(FakeExchange()))
    sibling = Sibling(client, "http://user.test", "user", HOME)

    with pytest.raises(RateLimitedError) as caught:
        await sibling.send("GET", "/v1/home")
    assert caught.value.retry_after == 7
    assert len(seen) == 1


async def test_a_header_a_capability_genuinely_needs_is_passed_through(make_client) -> None:
    handler, seen = recording(ok(json={}))
    client = make_client(handler, a_broker(FakeExchange()))

    await client.request(
        Call(method="POST", url=URL, audience=HOME, headers={"Idempotency-Key": "abc123"})
    )

    assert seen[0].headers["Idempotency-Key"] == "abc123"


async def test_the_call_decides_the_method_the_query_and_the_body(make_client) -> None:
    handler, seen = recording(ok(json={}))
    client = make_client(handler, a_broker(FakeExchange()))

    await client.request(
        Call(
            method="POST",
            url=URL,
            audience=HOME,
            json={"room": "kitchen"},
            params={"dry_run": "true"},
        )
    )

    assert seen[0].method == "POST"
    assert seen[0].url.params["dry_run"] == "true"
    assert seen[0].read() == b'{"room":"kitchen"}'


async def test_a_token_that_expired_in_flight_is_reminted_once_and_nobody_notices(
    make_client,
) -> None:
    # Tokens are minutes long by design, so this is ordinary rather than exceptional. The
    # person gets their answer; the only trace is a second mint.
    handler, seen = recording(refusing, ok(json={"rooms": 3}))
    exchange = FakeExchange()
    client = make_client(handler, a_broker(exchange))

    assert await client.request(Call(method="GET", url=URL, audience=HOME)) == {"rooms": 3}
    assert exchange.mints == 2
    assert seen[0].headers["Authorization"] == f"Bearer minted-{HOME}-1"
    assert seen[1].headers["Authorization"] == f"Bearer minted-{HOME}-2"


async def test_a_sibling_that_refuses_a_fresh_token_too_is_told_about_exactly_twice(
    make_client,
) -> None:
    # The test that would catch a retry loop: a sibling answering 401 forever costs two
    # requests and an honest error, not a wedged turn.
    handler, seen = recording(refusing)
    exchange = FakeExchange()
    client = make_client(handler, a_broker(exchange))

    with pytest.raises(DownstreamRefusedError) as refused:
        await client.request(Call(method="GET", url=URL, audience=HOME))

    assert len(seen) == 2
    assert exchange.mints == 2
    assert refused.value.status == 401
    assert refused.value.audience == HOME


async def test_a_refused_request_keeps_its_status_so_the_pack_can_say_what_it_means(
    make_client,
) -> None:
    # This layer will not guess. An absent playlist is a normal answer and an absent
    # endpoint is a deployment bug, and only the capability that asked knows which.
    handler, _ = recording(lambda: httpx.Response(404, json={"detail": "no such room"}))
    client = make_client(handler, a_broker(FakeExchange()))

    with pytest.raises(DownstreamRejectedError) as rejected:
        await client.request(Call(method="GET", url=URL, audience=HOME))

    assert rejected.value.status == 404
    assert rejected.value.detail == "no such room"
    assert rejected.value.retry_after is None


async def test_a_rate_limit_carries_the_interval_the_sibling_named(make_client) -> None:
    handler, _ = recording(
        lambda: httpx.Response(429, json=["slow down"], headers={"Retry-After": "30"})
    )
    client = make_client(handler, a_broker(FakeExchange()))

    with pytest.raises(DownstreamRejectedError) as limited:
        await client.request(Call(method="GET", url=URL, audience=HOME))

    assert limited.value.retry_after == 30.0
    # The body was JSON but not a problem document, so the text is the honest detail.
    assert limited.value.detail == '["slow down"]'


async def test_a_retry_after_given_as_a_date_is_not_turned_into_an_invented_interval(
    make_client,
) -> None:
    # Converting it against a clock that may disagree with the sender's is guessing, and a
    # caller must never invent an interval. Saying nothing is the honest answer.
    handler, _ = recording(
        lambda: httpx.Response(
            429,
            json={"title": "Too many requests"},
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        )
    )
    client = make_client(handler, a_broker(FakeExchange()))

    with pytest.raises(DownstreamRejectedError) as limited:
        await client.request(Call(method="GET", url=URL, audience=HOME))

    assert limited.value.retry_after is None
    assert limited.value.detail == '{"title":"Too many requests"}'


async def test_an_unwell_sibling_is_an_outage_rather_than_the_caller_s_fault(
    make_client,
) -> None:
    handler, _ = recording(lambda: httpx.Response(503, text="the vault is sealed"))
    client = make_client(handler, a_broker(FakeExchange()))

    with pytest.raises(DownstreamUnavailableError) as outage:
        await client.request(Call(method="GET", url=URL, audience=HOME))

    assert outage.value.status == 503
    assert outage.value.detail == "the vault is sealed"


async def test_a_sibling_that_cannot_be_reached_is_the_same_kind_of_failure(
    make_client,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = make_client(handler, a_broker(FakeExchange()))

    with pytest.raises(DownstreamUnavailableError) as outage:
        await client.request(Call(method="DELETE", url=URL, audience=HOME))

    assert outage.value.audience == HOME


async def test_an_answer_with_no_body_is_no_body_and_not_an_error(make_client) -> None:
    handler, _ = recording(lambda: httpx.Response(204))
    client = make_client(handler, a_broker(FakeExchange()))

    assert await client.request(Call(method="DELETE", url=URL, audience=HOME)) is None


async def test_an_answer_that_is_not_json_is_unintelligible_rather_than_half_understood(
    make_client,
) -> None:
    # A proxy's error page answering 200 must not become a tool result the model reasons from.
    handler, _ = recording(ok(text="<html>hello</html>"))
    client = make_client(handler, a_broker(FakeExchange()))

    with pytest.raises(DownstreamUnavailableError) as unreadable:
        await client.request(Call(method="GET", url=URL, audience=HOME))

    assert unreadable.value.detail == MALFORMED


async def test_a_capability_is_handed_these_two_and_no_credential_at_all(make_client) -> None:
    # PackContext reaches weftai's formatter, so nothing secret may be reachable from it.
    # What a capability holds is a broker and a client, both of which mint on demand.
    handler, seen = recording(ok(json={"rooms": 3}))
    context = PackContext(
        account_id="acct_a",
        profile="personal",
        session_id="ses_1",
        http=make_client(handler, a_broker(FakeExchange())),
        tokens=a_broker(FakeExchange()),
    )

    body = await context.http.request(Call(method="GET", url=URL, audience=HOME))

    assert body == {"rooms": 3}
    assert await context.tokens.token_for(HOME) == f"minted-{HOME}-1"
    assert CALLER_TOKEN not in repr(context)
    assert seen[0].headers["Authorization"].startswith("Bearer minted-")


async def test_a_retryable_sibling_is_retried_up_to_the_budget() -> None:
    handler, seen = recording(
        lambda: httpx.Response(503, json={"detail": "busy"}, headers={"Retry-After": "-1"}),
        ok(json={"rooms": 3}),
    )
    client = PackHttp(
        tokens=a_broker(FakeExchange()),
        transport=httpx.MockTransport(handler),
        retry_attempts=2,
        clock=lambda: 0.0,
        sleeper=_no_wait,
    )
    try:
        assert await client.request(Call(method="GET", url=URL, audience=HOME)) == {"rooms": 3}
    finally:
        await client.aclose()
    assert len(seen) == 2


async def test_zero_retry_attempts_leave_the_first_failure_as_the_answer() -> None:
    handler, seen = recording(lambda: httpx.Response(503, json={"detail": "busy"}))
    client = PackHttp(
        tokens=a_broker(FakeExchange()),
        transport=httpx.MockTransport(handler),
        retry_attempts=0,
        clock=lambda: 0.0,
        sleeper=_no_wait,
    )
    try:
        with pytest.raises(DownstreamUnavailableError):
            await client.request(Call(method="GET", url=URL, audience=HOME))
    finally:
        await client.aclose()
    assert len(seen) == 1


async def test_a_401_is_never_retried_as_a_transient_failure(make_client) -> None:
    handler, seen = recording(refusing)
    client = make_client(handler, a_broker(FakeExchange()))
    client.retry_attempts = 4
    with pytest.raises(DownstreamRefusedError):
        await client.request(Call(method="GET", url=URL, audience=HOME))
    assert len(seen) == 2


async def test_a_turn_policy_copies_timeout_and_retries_onto_the_client() -> None:
    handler, seen = recording(
        lambda: httpx.Response(429, headers={"Retry-After": "1"}, json={"detail": "slow"}),
        ok(json={"ok": True}),
    )
    client = PackHttp(
        tokens=a_broker(FakeExchange()),
        transport=httpx.MockTransport(handler),
        clock=lambda: 0.0,
        sleeper=_no_wait,
    )
    try:
        apply_downstream_policy(
            SimpleNamespace(inner=client),
            TurnPolicy(retry_attempts=1, retry_max_seconds=12, downstream_timeout_seconds=4),
        )
        apply_downstream_policy(object(), TurnPolicy())
        client.apply_policy(SimpleNamespace(retry_attempts=True, downstream_timeout_seconds="no"))
        assert await client.request(Call(method="GET", url=URL, audience=HOME)) == {"ok": True}
    finally:
        await client.aclose()
    assert len(seen) == 2
    assert client.retry_attempts == 1
    assert client.retry_max_seconds == 12
    assert client._timeout_seconds == 4.0


async def test_retries_stop_once_the_retry_window_has_elapsed() -> None:
    times = iter((0.0, 100.0, 100.0))
    handler, seen = recording(lambda: httpx.Response(500, json={"detail": "down"}))
    client = PackHttp(
        tokens=a_broker(FakeExchange()),
        transport=httpx.MockTransport(handler),
        retry_attempts=5,
        retry_max_seconds=30,
        clock=lambda: next(times, 100.0),
        sleeper=_no_wait,
    )
    try:
        with pytest.raises(DownstreamUnavailableError):
            await client.request(Call(method="GET", url=URL, audience=HOME))
    finally:
        await client.aclose()
    assert len(seen) == 1


async def test_a_connection_error_is_retried_like_a_5xx() -> None:
    seen = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ok": True})

    client = PackHttp(
        tokens=a_broker(FakeExchange()),
        transport=httpx.MockTransport(handler),
        retry_attempts=1,
        clock=lambda: 0.0,
        sleeper=_no_wait,
    )
    try:
        assert await client.request(Call(method="GET", url=URL, audience=HOME)) == {"ok": True}
    finally:
        await client.aclose()
    assert seen["n"] == 2


async def test_a_connection_error_with_no_retries_is_the_answer() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = PackHttp(
        tokens=a_broker(FakeExchange()),
        transport=httpx.MockTransport(handler),
        retry_attempts=0,
        clock=lambda: 0.0,
        sleeper=_no_wait,
    )
    try:
        with pytest.raises(DownstreamUnavailableError):
            await client.request(Call(method="GET", url=URL, audience=HOME))
    finally:
        await client.aclose()


EXEC_URL = "http://environments.test/v1/exec"
PLAY_URL = "http://spotify.test/v1/player/play"


@pytest.fixture
async def make_retrying_client() -> AsyncIterator[Callable[..., PackHttp]]:
    """Pack clients with two retries, a clock that never runs out and a sleep that never waits."""
    built: list[PackHttp] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> PackHttp:
        client = PackHttp(
            tokens=a_broker(FakeExchange()),
            transport=httpx.MockTransport(handler),
            retry_attempts=2,
            clock=lambda: 0.0,
            sleeper=_no_wait,
        )
        built.append(client)
        return client

    yield factory
    for client in built:
        await client.aclose()


def failing(error: type[httpx.TransportError]) -> Callable[[], httpx.Response]:
    def answer() -> httpx.Response:
        raise error("the sibling went quiet")

    return answer


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError]
)
async def test_a_write_that_may_have_reached_the_sibling_is_never_sent_twice(
    make_retrying_client, method: str, error: type[httpx.TransportError]
) -> None:
    """The bug, named: a ``workspace.run`` whose answer was slower than its call timeout was
    POSTed to ``/v1/exec`` again, and the command ran twice.

    The body is Environments-api's ``ExecOnceRequest`` (app/api/schemas.py), because the
    request that was repeated is the one that runs somebody's command.
    """
    handler, seen = recording(failing(error), ok(json={"exit_code": 0}))
    client = make_retrying_client(handler)

    with pytest.raises(DownstreamUnavailableError):
        await client.request(
            Call(
                method=method,
                url=EXEC_URL,
                audience=HOME,
                json={"environment_id": "env_1", "command": "make deploy", "timeout_ms": 5000},
            )
        )

    assert len(seen) == 1


async def test_spotify_s_confirmation_timeout_is_answered_rather_than_played_again(
    make_retrying_client,
) -> None:
    """The bug, named: Spotify-api's 504 means "accepted, not yet confirmed", and the hub
    answered it by POSTing play again, which restarted the track that was already playing.

    The body is what Spotify-api's ``problem_response`` (api/errors.py) renders for a
    ``ConfirmationTimeoutError`` (errors.py), with the observed state the model should see.
    """
    confirmation_timeout = {
        "type": "https://spotify-api.invalid/problems/confirmation-timeout",
        "title": "Gateway timeout",
        "status": 504,
        "detail": "the command was accepted but its effect could not be confirmed within 15s",
        "instance": "/v1/player/play",
        "details": {"observed": {"is_playing": True, "progress_ms": 1200}},
    }
    handler, seen = recording(lambda: httpx.Response(504, json=confirmation_timeout))
    client = make_retrying_client(handler)

    response = await client.request_response(
        Call(method="POST", url=PLAY_URL, audience=HOME, json={"uris": ["spotify:track:1"]})
    )

    assert response.status_code == 504
    assert response.json()["details"]["observed"]["is_playing"] is True
    assert len(seen) == 1


@pytest.mark.parametrize(("method", "status"), [("POST", 500), ("PATCH", 503)])
async def test_a_write_the_sibling_failed_on_is_left_as_the_answer(
    make_retrying_client, method: str, status: int
) -> None:
    # A 5xx says nothing about how far the sibling got before it failed.
    handler, seen = recording(lambda: httpx.Response(status, json={"detail": "broke"}))
    client = make_retrying_client(handler)

    with pytest.raises(DownstreamUnavailableError) as outage:
        await client.request(Call(method=method, url=URL, audience=HOME))

    assert outage.value.status == status
    assert len(seen) == 1


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_a_write_that_never_left_is_retried_because_nothing_can_have_happened(
    make_retrying_client, error: type[httpx.TransportError]
) -> None:
    handler, seen = recording(failing(error), ok(json={"exit_code": 0}))
    client = make_retrying_client(handler)

    body = await client.request(Call(method="POST", url=EXEC_URL, audience=HOME))

    assert body == {"exit_code": 0}
    assert len(seen) == 2


async def test_a_rate_limited_write_is_retried_because_the_sibling_said_it_did_nothing(
    make_retrying_client,
) -> None:
    handler, seen = recording(
        lambda: httpx.Response(429, json={"detail": "slow"}, headers={"Retry-After": "1"}),
        ok(json={"ok": True}),
    )
    client = make_retrying_client(handler)

    assert await client.request(Call(method="POST", url=URL, audience=HOME)) == {"ok": True}
    assert len(seen) == 2


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE", "put"])
async def test_an_idempotent_request_is_retried_after_a_timeout_and_an_outage(
    make_retrying_client, method: str
) -> None:
    # Sending one of these twice has the effect of sending it once, so a late answer or a
    # 5xx costs a repeat rather than a failed turn. The method is compared case-blind.
    handler, seen = recording(
        failing(httpx.ReadTimeout),
        lambda: httpx.Response(503, json={"detail": "busy"}),
        ok(),
    )
    client = make_retrying_client(handler)

    response = await client.request_response(Call(method=method, url=URL, audience=HOME))

    assert response.status_code == 200
    assert len(seen) == 3


async def _no_wait(_seconds: float) -> None:
    return


def test_retry_after_values_that_cannot_be_waited_are_treated_as_no_wait() -> None:
    from lucy_api.packs.http import _backoff

    assert _backoff(-1.0, 30) == 0.0
    assert _backoff(None, 30) == 0.0
    assert _backoff(5.0, 3.0) == 3.0
