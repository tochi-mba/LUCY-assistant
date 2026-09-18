"""Connection APIs expose metadata and subject-bound consent, never credentials."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from starlette.requests import Request

from lucy_api.api.dependencies import ActingAs
from lucy_api.api.routers.connections import (
    authorize_connection,
    delete_connection,
    get_connection,
    list_connections,
    open_connection_ticket,
    poll_connection_authorization,
)
from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.keyring import Connection, FakeKeyringClient
from lucy_api.connections.tickets import ConnectionTickets
from lucy_api.core.errors import LucyError

if TYPE_CHECKING:
    from lucy_api.core.container import PackRequest


@dataclass
class StubCapabilities:
    forgotten: list[tuple[str, str]] = field(default_factory=list)

    def forget_probes(self, account_id: str, profile: str, pack_id: str | None = None) -> None:
        del pack_id
        self.forgotten.append((account_id, profile))


@dataclass
class StubContainer:
    keyring: FakeKeyringClient = field(default_factory=FakeKeyringClient)
    connection_tickets: ConnectionTickets = field(
        default_factory=lambda: ConnectionTickets(issue=lambda: "ticket-1")
    )
    requests: list[PackRequest] = field(default_factory=list)
    capabilities: StubCapabilities = field(default_factory=StubCapabilities)

    def connection_client(self, request: PackRequest) -> FakeKeyringClient:
        self.requests.append(request)
        return self.keyring


def _acting(account_id: str = "acct_a") -> ActingAs:
    return ActingAs(VerifiedCaller(account_id=account_id, audience="lucy-api"), "user-jwt")


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/connections/spotify/authorize",
            "raw_path": b"/v1/connections/spotify/authorize",
            "query_string": b"",
            "headers": [],
            "server": ("lucy.test", 443),
            "client": ("127.0.0.1", 1234),
        }
    )


@pytest.mark.asyncio
async def test_connection_lifecycle_is_scoped_to_the_verified_subject() -> None:
    container = StubContainer()
    acting = _acting()

    started = await authorize_connection("spotify", acting, container, _request())

    assert started.connect_url == "https://lucy.test/connect?ticket=ticket-1"
    assert started.poll_url.endswith("/v1/connections/spotify/authorize/ticket-1")
    assert container.keyring.authorized == [("personal", "spotify")]
    assert container.requests[-1].caller.account_id == "acct_a"
    assert container.requests[-1].user_token == "user-jwt"
    pending = await poll_connection_authorization("spotify", "ticket-1", acting, container)
    assert pending.status == "authorization_pending"

    container.keyring.seed(
        "personal",
        [Connection(service="spotify", status="active", scopes=("playback-read",))],
    )
    listed = await list_connections(acting, container)
    found = await get_connection("spotify", acting, container)
    connected = await poll_connection_authorization("spotify", "ticket-1", acting, container)

    assert listed.data == [found]
    assert found.scopes == ["playback-read"]
    assert connected.status == "active"

    response = await delete_connection("spotify", acting, container)
    assert response.status_code == 204
    assert container.keyring.disconnected == [("personal", "spotify")]
    assert ("acct_a", "personal") in container.capabilities.forgotten


@pytest.mark.asyncio
async def test_consent_redirect_is_one_time_and_bound_to_the_subject() -> None:
    container = StubContainer()
    started = await authorize_connection("spotify", _acting(), container, _request())

    with pytest.raises(LucyError) as foreign:
        await open_connection_ticket(
            started.ticket, VerifiedCaller("acct_b", "lucy-api"), container
        )
    assert foreign.value.status == 404

    redirect = await open_connection_ticket(
        started.ticket, VerifiedCaller("acct_a", "lucy-api"), container
    )
    assert redirect.status_code == 303
    assert redirect.headers["location"] == container.keyring.connect_url

    with pytest.raises(LucyError) as reused:
        await open_connection_ticket(
            started.ticket, VerifiedCaller("acct_a", "lucy-api"), container
        )
    assert reused.value.status == 409


@pytest.mark.asyncio
async def test_absent_or_wrong_service_connection_is_not_disclosed() -> None:
    container = StubContainer()
    acting = _acting()

    with pytest.raises(LucyError) as missing:
        await get_connection("spotify", acting, container)
    assert missing.value.status == 404

    started = await authorize_connection("spotify", acting, container, _request())
    with pytest.raises(LucyError) as wrong_service:
        await poll_connection_authorization("calendar", started.ticket, acting, container)
    assert wrong_service.value.status == 404


class BrokenKeyring:
    async def connections(self, profile: str) -> tuple[object, ...]:
        del profile
        raise DownstreamError("keyring", 503)

    async def authorize(self, profile: str, service: str) -> object:
        del profile, service
        raise DownstreamError("keyring", 503)

    async def disconnect(self, profile: str, service: str) -> None:
        del profile, service
        raise DownstreamError("keyring", 503)


@dataclass
class BrokenContainer:
    connection_tickets: ConnectionTickets = field(
        default_factory=lambda: ConnectionTickets(issue=lambda: "ticket-1")
    )

    def connection_client(self, request: PackRequest) -> BrokenKeyring:
        del request
        return BrokenKeyring()


@pytest.mark.asyncio
async def test_a_keyring_outage_is_a_retryable_unavailable() -> None:
    container = BrokenContainer()
    acting = _acting()

    with pytest.raises(LucyError) as listed:
        await list_connections(acting, container)
    assert listed.value.status == 503
    with pytest.raises(LucyError) as started:
        await authorize_connection("spotify", acting, container, _request())
    assert started.value.status == 503
    with pytest.raises(LucyError) as removed:
        await delete_connection("spotify", acting, container)
    assert removed.value.status == 503


def test_forgetting_probes_is_a_no_op_when_the_container_has_no_cache() -> None:
    from lucy_api.api.routers.connections import _forget_probes

    _forget_probes(object(), "acct_a", "personal")
