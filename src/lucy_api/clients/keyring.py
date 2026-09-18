"""The connection status of every capability, in one call.

This is the cheapest probe in the hub and the reason the others are rarely needed: one
`GET /v1/profiles/{name}` reports, for one person and one profile, which services are
connected and whether each connection still works. Probing each capability at its own
service would be eight round trips on a turn that has not started yet.

**Nothing here reads a credential.** keyring has an internal route that resolves one, and
this client deliberately cannot reach it: the hub sits next to a vault, and the way a vault
stays a vault is that the process assembling a prompt has no code path to its contents. A
secret leaves keyring exactly once, as a header attached by the service that needed it.
`keyring_client` is what does that, at a different seam, for a different caller.

The projection drops what keyring is happy to tell us and Lucy has no use for: the
credential *kind*, whether a TOTP seed sits beside a password, creation timestamps, and the
delegation grants. A status, an expiry, the granted scopes and the last error are what a
probe reads and what a person is shown.

**Granted scopes, never requested ones.** `scopes` here is what the provider actually gave.
A partial-consent screen returns fewer than were asked for, and a capability that compared
against the request would report itself ready and then fail on the operation the person
declined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx
from keyring_client import USER_TOKEN_HEADER

from lucy_api.clients.errors import AbsentError, UnavailableError, raise_for
from lucy_api.clients.transport import Sibling, field, moment, rows, segment, text

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from lucy_api.packs.context import Http

SERVICE = "keyring"
AUDIENCE = "keyring-api"

ACTIVE = "active"
"""The one connection status that means a capability can be used right now."""


@dataclass(frozen=True, slots=True)
class Connection:
    """One service a profile is connected to, as a probe needs it.

    `status` is keyring's own word -- `active`, `pending`, `expired`, `revoked` -- and is
    left in that vocabulary rather than mapped to a capability state here. The mapping
    belongs to the pack, which knows what its own `insufficient_scope` looks like and which
    scopes it needed; a client that guessed would have to guess for eight capabilities.
    """

    service: str
    status: str
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    last_error: str = ""

    @property
    def usable(self) -> bool:
        """Whether this connection can produce a credential without anybody present."""
        return self.status == ACTIVE

    def missing(self, wanted: Iterable[str]) -> tuple[str, ...]:
        """Which of `wanted` the provider did not grant, in the order they were asked for.

        The answer to partial consent, and the reason `scopes` is carried at all.
        """
        granted = set(self.scopes)
        return tuple(scope for scope in wanted if scope not in granted)


@dataclass(frozen=True, slots=True)
class Authorization:
    """Where to send somebody to consent, and how long that link lasts."""

    url: str
    expires_at: datetime | None = None


class KeyringClient(Protocol):
    """What the hub asks of keyring: connection status, consent, and disconnection."""

    async def connections(self, profile: str) -> tuple[Connection, ...]:
        """Every service this profile is connected to. An unknown profile has none."""
        ...

    async def authorize(self, profile: str, service: str) -> Authorization:
        """Begin an OAuth authorization and return the link to open."""
        ...

    async def disconnect(self, profile: str, service: str) -> None:
        """Delete the stored credential for one service. Absent is already done."""
        ...


class HttpKeyringClient:
    """The real client, over the one seam a capability has to a sibling."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def connections(self, profile: str) -> tuple[Connection, ...]:
        """Every connection on one profile, or none at all.

        A profile nobody has created yet answers 404, and that is read as "no connections"
        rather than as a failure: a person who has never connected anything and a person
        whose profile was deleted are in the same position, and both should be offered
        setup rather than an error.
        """
        try:
            payload = await self._api.send("GET", f"/v1/profiles/{segment(profile)}")
        except AbsentError:
            return ()
        return tuple(_connection(row) for row in rows(payload, "connections"))

    async def authorize(self, profile: str, service: str) -> Authorization:
        """Ask keyring for a consent URL for one service on one profile."""
        path = f"/v1/profiles/{segment(profile)}/connections/{segment(service)}/authorize"
        payload = await self._api.send("POST", path)
        return Authorization(
            url=text(payload, "authorization_url"),
            expires_at=moment(field(payload, "expires_at")),
        )

    async def disconnect(self, profile: str, service: str) -> None:
        """Remove one connection, treating one that is already gone as success."""
        path = f"/v1/profiles/{segment(profile)}/connections/{segment(service)}"
        try:
            await self._api.send("DELETE", path)
        except AbsentError:
            return


class DelegatedKeyringClient:
    """Connection management through Keyring's two-credential internal boundary.

    ``Authorization`` proves that Lucy is the calling service. The signed user token is
    carried separately as subject proof and is never forwarded as a bearer credential.
    The client is request-scoped because that proof is request-scoped; its HTTP pool is
    owned by the process container.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        *,
        service_token: str,
        user_token: str,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {service_token}",
            USER_TOKEN_HEADER: user_token,
        }

    async def connections(self, profile: str) -> tuple[Connection, ...]:
        try:
            payload = await self._send("GET", f"/v1/internal/profiles/{segment(profile)}")
        except AbsentError:
            return ()
        return tuple(_connection(row) for row in rows(payload, "connections"))

    async def authorize(self, profile: str, service: str) -> Authorization:
        path = f"/v1/internal/profiles/{segment(profile)}/connections/{segment(service)}/authorize"
        payload = await self._send("POST", path)
        return Authorization(
            url=text(payload, "authorization_url"),
            expires_at=moment(field(payload, "expires_at")),
        )

    async def disconnect(self, profile: str, service: str) -> None:
        path = f"/v1/internal/profiles/{segment(profile)}/connections/{segment(service)}"
        try:
            await self._send("DELETE", path)
        except AbsentError:
            return

    async def _send(self, method: str, path: str) -> Any:
        try:
            response = await self._http.request(
                method, self._base_url + path, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise UnavailableError(SERVICE, 0, "the credential vault could not be reached") from exc
        raise_for(response, service=SERVICE)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise UnavailableError(SERVICE, response.status_code, "invalid response") from exc


def _connection(row: Any) -> Connection:
    """One connection, narrowed to what a probe and a person need."""
    return Connection(
        service=text(row, "service"),
        status=text(row, "status"),
        scopes=tuple(str(scope) for scope in rows(row, "scopes")),
        expires_at=moment(field(row, "expires_at")),
        last_error=text(row, "last_error"),
    )


class FakeKeyringClient:
    """An in-memory keyring, so a pack's tests can describe a person's connections.

    Hand-written and satisfying the real Protocol, so a change to the seam fails to type
    check rather than passing quietly. `authorized` and `disconnected` are recorded because
    what a setup flow *did* is the assertion a connection test wants, and there is no
    response to make it against.
    """

    def __init__(self) -> None:
        self.profiles: dict[str, tuple[Connection, ...]] = {}
        self.authorized: list[tuple[str, str]] = []
        self.disconnected: list[tuple[str, str]] = []
        self.connect_url = "https://lucy.test/connect?ticket=t-1"

    def seed(self, profile: str, connections: Iterable[Connection]) -> None:
        """Set what one profile is connected to, replacing whatever was there."""
        self.profiles[profile] = tuple(connections)

    async def connections(self, profile: str) -> tuple[Connection, ...]:
        """The seeded connections, or none: an unseeded profile is not an error."""
        return self.profiles.get(profile, ())

    async def authorize(self, profile: str, service: str) -> Authorization:
        """Record the request and hand back the fake's fixed link."""
        self.authorized.append((profile, service))
        return Authorization(url=self.connect_url)

    async def disconnect(self, profile: str, service: str) -> None:
        """Record the disconnection and drop the connection from the profile."""
        self.disconnected.append((profile, service))
        kept = tuple(item for item in self.profiles.get(profile, ()) if item.service != service)
        self.profiles[profile] = kept


if TYPE_CHECKING:

    def _satisfies(real: HttpKeyringClient, fake: FakeKeyringClient) -> tuple[KeyringClient, ...]:
        """Static proof that both implementations satisfy the seam.

        mypy checks `src` only, so asserting this in the suite would assert nothing, and a
        Protocol nothing is checked against is a Protocol that has already drifted.
        """
        return (real, fake)


__all__ = [
    "ACTIVE",
    "AUDIENCE",
    "SERVICE",
    "Authorization",
    "Connection",
    "DelegatedKeyringClient",
    "FakeKeyringClient",
    "HttpKeyringClient",
    "KeyringClient",
]
