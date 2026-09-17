"""Keyring's token exchange, spoken directly because the pinned client cannot speak it.

Two credentials go out on every exchange and that is the whole point of the endpoint. Lucy
presents its own service token, which says *which service is asking*, and the person's
verified ``aud=lucy-api`` token, which says *who it is asking for*. A service that could
authenticate as itself and then name whoever it liked would be the confused deputy moved
out of one process and into the gap between two, so keyring takes the account from the user
token and offers no parameter by which a caller can name one.

What comes back is a fresh token for exactly one downstream audience. Lucy therefore never
forwards a caller's token to a sibling, which MCP's authorization spec requires in those
words and which the family's own defence already assumes.

The pinned ``keyring_client`` exposes no exchange method -- its surface is credentials,
JWKS and token verification, and the endpoint (``exchange_user_token`` in Keyring-api's
delegation router) landed after the tag this repository pins. So the hub speaks to that one
endpoint itself with httpx. The header name is still imported from the client rather than
respelled here: it is the detail most likely to drift and the cheapest to keep honest.

Keyring's refusals are deliberately undifferentiated on the wire -- a wrong service token,
an expired user token and a revoked grant are all one 401, because telling them apart is an
oracle. The exceptions below are named for **who has to act**, which is the only
distinction a caller can use: the person signs in again, the operator fixes an allowlist,
or nobody yet because keyring is unwell and a retry may work.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx
from keyring_client import USER_TOKEN_HEADER

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

EXCHANGE_PATH = "/v1/internal/token-exchange"

DEFAULT_TIMEOUT_SECONDS = 5.0
"""Short, because an exchange sits on the critical path of somebody's turn."""

DEFAULT_TTL_SECONDS = 900
"""Fifteen minutes, which is keyring's own ceiling. It caps the request anyway; asking for
more would only make the answer quieter about which limit applied."""

DEFAULT_GRANT_TTL_SECONDS = 2_592_000

UNREACHABLE = "keyring could not be reached to exchange for a downstream token"
MALFORMED = "keyring's answer could not be understood"


class ExchangeError(Exception):
    """Base class for every refusal this module raises deliberately.

    Carries the status keyring answered with, or ``0`` when it never answered, so a caller
    that logs one failure can say what happened without re-deriving it from the type.
    """

    def __init__(self, detail: str, *, status: int = 0) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


class DelegationRefusedError(ExchangeError):
    """Keyring did not accept the delegation presented.

    The person's problem, usually: their token expired, or the grant was revoked. It is
    also what a wrong service token looks like, because keyring will not say which.
    """


class AudienceNotAllowedError(ExchangeError):
    """This service may not exchange for that audience.

    The operator's problem, and never worth a retry: keyring reads ``exchange_audiences``
    afresh on every exchange, so the answer will not change until the deployment does.
    """


class GrantNotFoundError(ExchangeError):
    """No such profile or grant for this account.

    Foreign and nonexistent are the same answer on purpose; a 403 here would confirm that
    somebody else owns the name.
    """


class ExchangeRejectedError(ExchangeError):
    """Keyring answered, refused, and it was none of the above -- a 422 or a 429."""


class KeyringUnavailableError(ExchangeError):
    """Keyring is unwell, unreachable or unintelligible. A retry may work."""


_STATUS_ERRORS: dict[int, type[ExchangeError]] = {
    httpx.codes.UNAUTHORIZED: DelegationRefusedError,
    httpx.codes.FORBIDDEN: AudienceNotAllowedError,
    httpx.codes.NOT_FOUND: GrantNotFoundError,
}


@dataclass(frozen=True, slots=True, repr=False)
class ExchangedToken:
    """Authority for one audience, for a few minutes.

    ``repr`` is hand-written because the realistic leak is not a log line somebody chose to
    write: it is a traceback printing the locals of the frame that happened to hold this.
    """

    audience: str
    token: str
    expires_in: int
    expires_at: datetime

    def __repr__(self) -> str:
        """Say what it is for and how long it lives. Never say what it is."""
        return (
            f"ExchangedToken(audience={self.audience!r}, token=<redacted>, "
            f"expires_in={self.expires_in!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True, slots=True)
class OfflineGrant:
    """Standing consent for background work, as keyring describes it back.

    Inspectable on purpose -- a person should be able to see every delegation they have
    given -- and safe to render, because the handle alone is not a credential: an exchange
    under it still requires the service's own token.
    """

    grant_id: str
    profile: str
    service: str
    audiences: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@runtime_checkable
class TokenExchange(Protocol):
    """The seam the broker sits on: one way to turn a delegation into one token.

    A Protocol rather than the class, so the broker's tests can substitute a hand-written
    fake without a transport, and so what a composition root depends on is the shape.
    """

    async def exchange(
        self,
        *,
        audience: str,
        user_token: str | None = None,
        grant_id: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> ExchangedToken: ...

    async def aclose(self) -> None: ...


class KeyringExchange:
    """An HTTP client for keyring's exchange and for the grants that stand behind it.

    Args:
        base_url: where keyring lives.
        service_token: this hub's own credential, proving which service is calling.
        timeout_seconds: per-request timeout, short by default. See
            :data:`DEFAULT_TIMEOUT_SECONDS`.
        transport: an httpx transport, so a test can drive the real client in-process.
    """

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._service_token = service_token
        # Redirects are off: every request from here carries credentials, and a redirect is
        # somebody else's server asking for them.
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    async def exchange(
        self,
        *,
        audience: str,
        user_token: str | None = None,
        grant_id: str | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> ExchangedToken:
        """Mint a token for one downstream audience, for the delegation presented.

        Exactly one of ``user_token`` and ``grant_id`` is meant, and this client does not
        re-check that: keyring refuses both and neither with the same 401 it uses for every
        other bad delegation, and a second copy of the rule here would be a second place
        for it to drift. What is enforced locally is that a value which was not given is
        not sent -- an absent grant is an absent field rather than a null one, because
        keyring's request model forbids extras and reads a present field as an intention.

        Raises:
            DelegationRefusedError: keyring did not accept the delegation.
            AudienceNotAllowedError: this service may not exchange for that audience.
            ExchangeRejectedError: keyring refused for some other reason it named.
            KeyringUnavailableError: keyring is unreachable, unwell, or unintelligible.
        """
        payload: dict[str, Any] = {"audience": audience, "ttl_seconds": ttl_seconds}
        if grant_id is not None:
            payload["grant_id"] = grant_id
        headers = {"Authorization": f"Bearer {self._service_token}"}
        if user_token is not None:
            headers[USER_TOKEN_HEADER] = user_token

        body = await self._send("POST", EXCHANGE_PATH, headers=headers, json=payload)
        try:
            return ExchangedToken(
                audience=audience,
                token=str(body["token"]),
                expires_in=int(body["expires_in"]),
                expires_at=_moment(body["expires_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KeyringUnavailableError(MALFORMED) from exc

    async def create_grant(
        self,
        *,
        profile: str,
        session_token: str,
        service: str,
        audiences: Sequence[str],
        ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
    ) -> OfflineGrant:
        """Record a person's consent for background work, under their login session.

        The session token belongs to the person in front of a browser and is not Lucy's to
        keep: it is presented once, here, and stored nowhere. What Lucy keeps is the
        returned handle, which is why the authority stays in keyring, where they can revoke
        it without Lucy's cooperation.

        Raises:
            As :meth:`exchange`, plus ``GrantNotFoundError`` for an unknown profile.
        """
        body = await self._send(
            "POST",
            _grants_path(profile),
            headers=_session_headers(session_token),
            json={
                "service": service,
                "audiences": list(audiences),
                "ttl_seconds": ttl_seconds,
            },
        )
        return _grant_from(body)

    async def list_grants(self, *, profile: str, session_token: str) -> tuple[OfflineGrant, ...]:
        """Every delegation recorded for this profile, revoked and expired ones included.

        History is part of the answer. A consent screen showing only live grants cannot
        answer "did I ever let it do that?", which is the question people actually ask.

        Raises:
            As :meth:`create_grant`.
        """
        body = await self._send(
            "GET", _grants_path(profile), headers=_session_headers(session_token)
        )
        try:
            grants = list(body["grants"])
        except (KeyError, TypeError) as exc:
            raise KeyringUnavailableError(MALFORMED) from exc
        return tuple(_grant_from(grant) for grant in grants)

    async def revoke_grant(self, *, profile: str, session_token: str, grant_id: str) -> None:
        """Stop future exchanges under one grant. Idempotent at keyring, and quiet here.

        Tokens already minted under it stay valid until they expire, which is minutes and
        not a design flaw: the alternative is a revocation check on every downstream call,
        which is the network hop this family is arranged to avoid.

        Raises:
            As :meth:`create_grant`.
        """
        await self._send(
            "DELETE",
            f"{_grants_path(profile)}/{quote(grant_id, safe='')}",
            headers=_session_headers(session_token),
        )

    async def aclose(self) -> None:
        """Release the connection pool."""
        await self._http.aclose()

    async def _send(
        self, method: str, path: str, *, headers: Mapping[str, str], json: Any = None
    ) -> Any:
        """One request, with keyring's answer classified into this module's vocabulary."""
        try:
            response = await self._http.request(method, path, headers=dict(headers), json=json)
        except httpx.HTTPError as exc:
            raise KeyringUnavailableError(UNREACHABLE) from exc

        _raise_for(response)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise KeyringUnavailableError(MALFORMED, status=response.status_code) from exc


def _raise_for(response: httpx.Response) -> None:
    """Turn a refusal into the exception that names whoever has to act about it."""
    status = response.status_code
    if status < httpx.codes.BAD_REQUEST:
        return
    detail = _detail_of(response)
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise KeyringUnavailableError(detail, status=status)
    raise _STATUS_ERRORS.get(status, ExchangeRejectedError)(detail, status=status)


def _detail_of(response: httpx.Response) -> str:
    """The ``detail`` out of keyring's problem+json, or something honest when there is none."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else response.text[:200]


def _grants_path(profile: str) -> str:
    """Percent-encoded, so a profile name cannot climb out of the path it is placed in."""
    return f"/v1/profiles/{quote(profile, safe='')}/grants"


def _session_headers(session_token: str) -> dict[str, str]:
    """The person's own login session. Not the service token: this is their decision."""
    return {"Authorization": f"Bearer {session_token}"}


def _grant_from(body: Any) -> OfflineGrant:
    """Read one grant, or say that keyring's answer could not be understood."""
    try:
        revoked = body["revoked_at"]
        return OfflineGrant(
            grant_id=str(body["grant_id"]),
            profile=str(body["profile"]),
            service=str(body["service"]),
            audiences=tuple(str(audience) for audience in body["audiences"]),
            created_at=_moment(body["created_at"]),
            expires_at=_moment(body["expires_at"]),
            revoked_at=None if revoked is None else _moment(revoked),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise KeyringUnavailableError(MALFORMED) from exc


def _moment(value: Any) -> datetime:
    """Read an ISO instant, and read a naive one as UTC rather than as local time.

    Keyring signs in UTC and serialises an offset, so the naive arm should never fire. It
    exists because the alternative when it does is a datetime that silently means a
    different instant on every machine that reads it.
    """
    moment = datetime.fromisoformat(str(value))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


__all__ = [
    "AudienceNotAllowedError",
    "DelegationRefusedError",
    "ExchangeError",
    "ExchangeRejectedError",
    "ExchangedToken",
    "GrantNotFoundError",
    "KeyringExchange",
    "KeyringUnavailableError",
    "OfflineGrant",
    "TokenExchange",
]
