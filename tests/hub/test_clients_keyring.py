"""The probe that decides, for every capability at once, whether it exists this turn.

Three things are pinned here. A profile nobody has created is "nothing is connected", not a
failure -- the person who has never connected anything and the person whose profile was
deleted are both offered setup. The projection carries a status and never a credential, and
never the shape of one. And `scopes` is what the provider granted, so a partial consent is
visible rather than discovered on the first operation the person declined.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import pytest
from keyring_client import USER_TOKEN_HEADER

from lucy_api.clients.errors import UnavailableError
from lucy_api.clients.keyring import (
    PENDING,
    Connection,
    DelegatedKeyringClient,
    FakeKeyringClient,
    HttpKeyringClient,
)
from lucy_api.clients.testing import Answer, FakeHttp, problem

PROFILE = "personal"

CONNECTED = {
    "name": PROFILE,
    "created_at": "2026-01-04T09:12:00Z",
    "updated_at": "2026-03-02T18:40:00Z",
    "connections": [
        {
            "service": "spotify",
            "kind": "oauth2_authorization_code",
            "status": "active",
            "created_at": "2026-01-04T09:12:00Z",
            "updated_at": "2026-03-02T18:40:00Z",
            "expires_at": "2026-09-10T13:00:00Z",
            "scopes": ["user-read-private", "user-modify-playback-state"],
            "stores_totp_seed": True,
            "last_error": None,
        }
    ],
    "grants": [{"grant_id": "g-1", "audiences": ["user.home"]}],
}


def client(*answers: Answer) -> tuple[HttpKeyringClient, FakeHttp]:
    http = FakeHttp(*answers)
    return HttpKeyringClient(http, "http://keyring.test"), http


async def test_one_read_reports_every_service_this_person_has_connected() -> None:
    keyring, http = client(Answer(body=CONNECTED))

    connections = await keyring.connections(PROFILE)

    assert http.last.url == "http://keyring.test/v1/profiles/personal"
    assert http.last.audience == "keyring-api"
    assert connections == (
        Connection(
            service="spotify",
            status="active",
            scopes=("user-read-private", "user-modify-playback-state"),
            expires_at=datetime(2026, 9, 10, 13, 0, tzinfo=UTC),
        ),
    )


async def test_the_projection_carries_a_status_and_nothing_that_describes_a_credential() -> None:
    keyring, _ = client(Answer(body=CONNECTED))

    (connection,) = await keyring.connections(PROFILE)

    assert set(asdict(connection)) == {"service", "status", "scopes", "expires_at", "last_error"}
    assert "stores_totp_seed" not in asdict(connection)
    assert "kind" not in asdict(connection)


async def test_a_profile_nobody_has_created_is_nothing_connected_rather_than_a_failure() -> None:
    keyring, _ = client(problem(404, detail="no such profile"))

    assert await keyring.connections(PROFILE) == ()


async def test_an_outage_at_the_vault_is_still_an_outage_and_is_not_read_as_absence() -> None:
    keyring, _ = client(problem(503, detail="sealed"))

    with pytest.raises(UnavailableError):
        await keyring.connections(PROFILE)


async def test_a_connection_that_needs_reauthorising_says_why_the_last_refresh_failed() -> None:
    body = {"connections": [{"service": "spotify", "status": "expired", "last_error": "revoked"}]}
    keyring, _ = client(Answer(body=body))

    (connection,) = await keyring.connections(PROFILE)

    assert connection.usable is False
    assert connection.last_error == "revoked"


def test_partial_consent_is_visible_because_the_granted_scopes_are_the_authority() -> None:
    connection = Connection(service="spotify", status="active", scopes=("user-read-private",))

    assert connection.usable is True
    assert connection.missing(["user-read-private", "user-modify-playback-state"]) == (
        "user-modify-playback-state",
    )


async def test_asking_for_consent_returns_the_link_to_open_and_when_it_stops_working() -> None:
    body = {"authorization_url": "https://provider.test/auth", "expires_at": "2026-09-10T13:00:00Z"}
    keyring, http = client(Answer(body=body))

    authorization = await keyring.authorize(PROFILE, "spotify")

    assert http.last.method == "POST"
    assert http.last.url.endswith("/v1/profiles/personal/connections/spotify/authorize")
    assert authorization.url == "https://provider.test/auth"
    assert authorization.expires_at == datetime(2026, 9, 10, 13, 0, tzinfo=UTC)


async def test_disconnecting_something_that_is_already_gone_is_success_not_a_404() -> None:
    keyring, http = client(Answer(status_code=204), problem(404))

    await keyring.disconnect(PROFILE, "spotify")
    await keyring.disconnect(PROFILE, "spotify")

    assert http.calls[0].method == "DELETE"
    assert http.calls[0].url.endswith("/v1/profiles/personal/connections/spotify")


async def test_the_fake_describes_a_persons_connections_without_a_service_to_describe_them() -> (
    None
):
    fake = FakeKeyringClient()
    fake.seed(PROFILE, [Connection(service="spotify", status="active")])

    assert await fake.connections(PROFILE) == (Connection(service="spotify", status="active"),)
    assert await fake.connections("work") == ()


async def test_the_fake_records_what_a_setup_flow_did_because_there_is_no_answer_to_read() -> None:
    fake = FakeKeyringClient()
    fake.seed(PROFILE, [Connection(service="spotify", status="active")])

    authorization = await fake.authorize(PROFILE, "spotify")
    await fake.disconnect(PROFILE, "spotify")

    assert authorization.url == fake.connect_url
    assert fake.authorized == [(PROFILE, "spotify")]
    assert fake.disconnected == [(PROFILE, "spotify")]
    assert await fake.connections(PROFILE) == ()


async def test_the_fake_leaves_the_pending_row_keyring_writes_before_handing_out_a_link() -> None:
    """The bug, named: the fake wrote nothing, so the connection poll's test never met the row
    the real vault always has by then. keyring writes `status=ConnectionStatus.PENDING` before
    it returns the link, and replaces an earlier placeholder rather than adding a second
    (`Keyring-api/src/keyring_api/credentials/service.py:244-258`)."""
    fake = FakeKeyringClient()

    await fake.authorize(PROFILE, "spotify")
    await fake.authorize(PROFILE, "spotify")

    assert await fake.connections(PROFILE) == (Connection(service="spotify", status=PENDING),)


async def test_the_fake_marks_a_connection_that_stopped_working_as_waiting_again() -> None:
    """keyring keeps only a working connection; an expired one reads `pending` while the new
    consent is under way, so the poll does not answer `expired` the moment it starts."""
    fake = FakeKeyringClient()
    fake.seed(PROFILE, [Connection(service="spotify", status="expired")])

    await fake.authorize(PROFILE, "spotify")

    assert await fake.connections(PROFILE) == (Connection(service="spotify", status=PENDING),)


async def test_the_fake_starting_consent_again_does_not_stop_a_connection_that_works() -> None:
    """keyring writes its placeholder only where no real connection stands, because one over
    a working connection turned it `pending` for good if the person closed the page."""
    fake = FakeKeyringClient()
    working = Connection(service="spotify", status="active", scopes=("user-read-private",))
    fake.seed(PROFILE, [working])

    await fake.authorize(PROFILE, "spotify")

    assert await fake.connections(PROFILE) == (working,)


async def test_the_delegated_client_uses_two_credentials_and_internal_paths() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=CONNECTED)
        if request.method == "POST":
            return httpx.Response(200, json={"authorization_url": "https://provider.test"})
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        delegated = DelegatedKeyringClient(
            http,
            "http://keyring.test",
            service_token="service-secret",
            user_token="signed-user-proof",
        )
        assert (await delegated.connections(PROFILE))[0].service == "spotify"
        assert (await delegated.authorize(PROFILE, "spotify")).url == "https://provider.test"
        await delegated.disconnect(PROFILE, "spotify")

    assert [request.url.path for request in seen] == [
        "/v1/internal/profiles/personal",
        "/v1/internal/profiles/personal/connections/spotify/authorize",
        "/v1/internal/profiles/personal/connections/spotify",
    ]
    for request in seen:
        assert request.headers["Authorization"] == "Bearer service-secret"
        assert request.headers[USER_TOKEN_HEADER] == "signed-user-proof"


async def test_the_delegated_client_reads_absent_profiles_and_connections_idempotently() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"detail": "not found"})
        )
    ) as http:
        delegated = DelegatedKeyringClient(
            http, "http://keyring.test", service_token="service", user_token="user"
        )
        assert await delegated.connections(PROFILE) == ()
        await delegated.disconnect(PROFILE, "spotify")
