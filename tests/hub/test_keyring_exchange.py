"""What Lucy sends to keyring's exchange, and what it makes of every answer.

Two things are being pinned. The first is the wire: two credentials, one audience, and a
field that was not given is a field that is not sent. The second is the classification --
keyring refuses in one undifferentiated way on purpose, so the value this client adds is
naming who has to act, and every arm of that naming is exercised below.

Everything runs over `httpx.MockTransport`. No network, no keyring, no port.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from lucy_api.auth.exchange import (
    MALFORMED,
    AudienceNotAllowedError,
    DelegationRefusedError,
    ExchangeRejectedError,
    GrantNotFoundError,
    KeyringExchange,
    KeyringUnavailableError,
    TokenExchange,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

BASE_URL = "http://keyring.test"
SERVICE_TOKEN = "lucy-service-token"
USER_TOKEN = "the.persons.aud-lucy-api.jwt"
SESSION_TOKEN = "the-persons-browser-session"
MINTED = "minted.for.user-home"

TOKEN_BODY = {
    "token": MINTED,
    "token_type": "Bearer",
    "expires_in": 300,
    "expires_at": "2026-09-17T12:00:00+00:00",
}

GRANT_BODY = {
    "grant_id": "dgt_abc",
    "profile": "personal",
    "service": "lucy-api",
    "audiences": ["user.home"],
    "created_at": "2026-09-17T12:00:00+00:00",
    "expires_at": "2026-10-17T12:00:00+00:00",
    "revoked_at": None,
}

Handler = "Callable[[httpx.Request], httpx.Response]"


@pytest.fixture
async def make_exchange() -> AsyncIterator[Callable[..., KeyringExchange]]:
    """Build clients over a mock transport and close every one of them afterwards."""
    built: list[KeyringExchange] = []

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> KeyringExchange:
        client = KeyringExchange(
            base_url=BASE_URL + "/",
            service_token=SERVICE_TOKEN,
            transport=httpx.MockTransport(handler),
        )
        built.append(client)
        return client

    yield factory
    for client in built:
        await client.aclose()


def answering(status: int, **kwargs: Any) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that always answers the same way, recording nothing."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **kwargs)

    return handler


async def test_an_exchange_presents_both_credentials_and_names_one_audience(
    make_exchange,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TOKEN_BODY)

    minted = await (make_exchange(handler)).exchange(
        audience="user.home", user_token=USER_TOKEN, ttl_seconds=300
    )

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/internal/token-exchange"
    assert request.headers["Authorization"] == f"Bearer {SERVICE_TOKEN}"
    assert request.headers["X-Keyring-User-Token"] == USER_TOKEN
    assert json.loads(request.content) == {"audience": "user.home", "ttl_seconds": 300}
    assert minted.token == MINTED
    assert minted.audience == "user.home"
    assert minted.expires_in == 300
    assert minted.expires_at.tzinfo is not None


async def test_an_offline_grant_travels_in_the_body_and_sends_no_user_token_header(
    make_exchange,
) -> None:
    # The background path presents a handle instead of a person. Sending both is what
    # keyring refuses, so a client that always sent the header would break offline work.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TOKEN_BODY)

    await (make_exchange(handler)).exchange(audience="user.home", grant_id="dgt_abc")

    assert "x-keyring-user-token" not in seen[0].headers
    assert json.loads(seen[0].content)["grant_id"] == "dgt_abc"


async def test_a_delegation_that_was_not_given_is_absent_rather_than_null(
    make_exchange,
) -> None:
    # Keyring's request model forbids extras and reads a present grant_id as an intention,
    # so "grant_id": null is a different request from no grant_id at all.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=TOKEN_BODY)

    await (make_exchange(handler)).exchange(audience="user.home", user_token=USER_TOKEN)

    assert "grant_id" not in json.loads(seen[0].content)
    assert json.loads(seen[0].content)["ttl_seconds"] == 900


async def test_a_refused_delegation_carries_keyring_s_own_sentence(make_exchange) -> None:
    client = make_exchange(
        answering(401, json={"detail": "the delegation was not accepted", "status": 401})
    )

    with pytest.raises(DelegationRefusedError) as refusal:
        await client.exchange(audience="user.home", user_token=USER_TOKEN)

    assert refusal.value.status == 401
    assert refusal.value.detail == "the delegation was not accepted"


async def test_an_audience_outside_the_allowlist_is_a_different_error_from_a_refusal(
    make_exchange,
) -> None:
    # A 403 is the operator's to fix and will not change on a retry; a 401 might. Folding
    # the two together would make a misconfigured allowlist look like an expired session.
    client = make_exchange(answering(403, json={"detail": "not that audience"}))

    with pytest.raises(AudienceNotAllowedError):
        await client.exchange(audience="user.vault", user_token=USER_TOKEN)


async def test_an_unknown_grant_is_not_found_and_says_nothing_about_who_owns_it(
    make_exchange,
) -> None:
    client = make_exchange(answering(404, json=["not", "a", "problem", "document"]))

    with pytest.raises(GrantNotFoundError) as absent:
        await client.list_grants(profile="personal", session_token=SESSION_TOKEN)

    # The body was JSON but not a problem document, so the raw text is the honest detail.
    assert absent.value.detail.startswith("[")


async def test_a_refusal_keyring_does_not_name_keeps_its_status(make_exchange) -> None:
    client = make_exchange(answering(422, json={"detail": 7}))

    with pytest.raises(ExchangeRejectedError) as rejected:
        await client.exchange(audience="user.home", user_token=USER_TOKEN)

    assert rejected.value.status == 422
    # `detail: 7` is not a sentence, so the client quotes the body rather than inventing one.
    assert rejected.value.detail == '{"detail":7}'


async def test_an_unwell_keyring_is_nobody_s_fault_yet(make_exchange) -> None:
    client = make_exchange(answering(503, text="the vault is sealed"))

    with pytest.raises(KeyringUnavailableError) as outage:
        await client.exchange(audience="user.home", user_token=USER_TOKEN)

    assert outage.value.status == 503
    assert outage.value.detail == "the vault is sealed"


async def test_a_keyring_that_cannot_be_reached_is_the_same_kind_of_failure(
    make_exchange,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(KeyringUnavailableError):
        await (make_exchange(handler)).exchange(audience="user.home", user_token=USER_TOKEN)


async def test_an_answer_that_is_not_json_is_unintelligible_rather_than_a_token(
    make_exchange,
) -> None:
    # A proxy's error page answering 200 must not become a token-shaped object.
    client = make_exchange(answering(200, text="<html>hello</html>"))

    with pytest.raises(KeyringUnavailableError) as unreadable:
        await client.exchange(audience="user.home", user_token=USER_TOKEN)

    assert unreadable.value.detail == MALFORMED


async def test_an_answer_missing_a_field_is_unintelligible_rather_than_half_read(
    make_exchange,
) -> None:
    client = make_exchange(answering(200, json={"token": MINTED}))

    with pytest.raises(KeyringUnavailableError):
        await client.exchange(audience="user.home", user_token=USER_TOKEN)


async def test_a_minted_token_never_prints_itself(make_exchange) -> None:
    # A traceback printing the locals of the frame that held this is the realistic leak.
    minted = await (make_exchange(answering(200, json=TOKEN_BODY))).exchange(
        audience="user.home", user_token=USER_TOKEN
    )

    assert MINTED not in repr(minted)
    assert "<redacted>" in repr(minted)
    assert "user.home" in repr(minted)


async def test_an_expiry_without_an_offset_is_read_as_utc_and_never_as_local_time(
    make_exchange,
) -> None:
    body = {**TOKEN_BODY, "expires_at": "2026-09-17T12:00:00"}
    minted = await (make_exchange(answering(200, json=body))).exchange(
        audience="user.home", user_token=USER_TOKEN
    )

    assert minted.expires_at.utcoffset() is not None
    assert minted.expires_at.utcoffset().total_seconds() == 0


async def test_creating_a_grant_uses_the_person_s_session_and_not_the_service_token(
    make_exchange,
) -> None:
    # Standing consent is the person's decision, so it is made under the person's own
    # login session. A service token here would be Lucy granting itself permission.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json=GRANT_BODY)

    grant = await (make_exchange(handler)).create_grant(
        profile="personal",
        session_token=SESSION_TOKEN,
        service="lucy-api",
        audiences=("user.home",),
        ttl_seconds=60,
    )

    assert seen[0].headers["Authorization"] == f"Bearer {SESSION_TOKEN}"
    assert SERVICE_TOKEN not in str(seen[0].headers)
    assert seen[0].url.path == "/v1/profiles/personal/grants"
    assert json.loads(seen[0].content) == {
        "service": "lucy-api",
        "audiences": ["user.home"],
        "ttl_seconds": 60,
    }
    assert grant.grant_id == "dgt_abc"
    assert grant.audiences == ("user.home",)
    assert grant.revoked_at is None


async def test_listing_grants_keeps_the_revoked_ones_so_the_history_is_answerable(
    make_exchange,
) -> None:
    revoked = {**GRANT_BODY, "grant_id": "dgt_old", "revoked_at": "2026-09-18T09:00:00+00:00"}
    client = make_exchange(answering(200, json={"grants": [GRANT_BODY, revoked]}))

    grants = await client.list_grants(profile="personal", session_token=SESSION_TOKEN)

    assert [grant.grant_id for grant in grants] == ["dgt_abc", "dgt_old"]
    assert grants[0].revoked_at is None
    assert grants[1].revoked_at is not None


async def test_a_grant_list_that_is_not_a_list_is_unintelligible(make_exchange) -> None:
    client = make_exchange(answering(200, json={"grants": 12}))

    with pytest.raises(KeyringUnavailableError):
        await client.list_grants(profile="personal", session_token=SESSION_TOKEN)


async def test_a_grant_missing_a_field_is_unintelligible(make_exchange) -> None:
    client = make_exchange(answering(201, json={"grant_id": "dgt_abc"}))

    with pytest.raises(KeyringUnavailableError):
        await client.create_grant(
            profile="personal",
            session_token=SESSION_TOKEN,
            service="lucy-api",
            audiences=("user.home",),
        )


async def test_revoking_a_grant_expects_no_body_back(make_exchange) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    assert (
        await (make_exchange(handler)).revoke_grant(
            profile="personal", session_token=SESSION_TOKEN, grant_id="dgt_abc"
        )
        is None
    )
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/v1/profiles/personal/grants/dgt_abc"


async def test_a_profile_name_cannot_climb_out_of_the_path_it_is_placed_in(
    make_exchange,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"grants": []})

    await (make_exchange(handler)).list_grants(
        profile="../../internal/token-exchange", session_token=SESSION_TOKEN
    )

    # `raw_path` is what goes on the wire; `path` is the decoded reading of it.
    assert seen[0].url.raw_path == b"/v1/profiles/..%2F..%2Finternal%2Ftoken-exchange/grants"


def test_the_client_satisfies_the_seam_the_broker_depends_on() -> None:
    # The Protocol is what a composition root names, so a method renamed here has to fail
    # somewhere rather than only at the first call in production.
    assert isinstance(
        KeyringExchange(base_url=BASE_URL, service_token=SERVICE_TOKEN), TokenExchange
    )
