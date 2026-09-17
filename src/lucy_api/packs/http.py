"""The one way a capability reaches a sibling, and the one place a token is attached.

Capabilities never build their own HTTP client and never see a credential. They describe a
call -- method, url, audience -- and this attaches authority for that audience and nothing
else. Concentrating it here is what makes the hub's central promise checkable in one file
rather than in every pack somebody writes afterwards.

**A caller's token is never forwarded.** Every outbound request is authorized with a token
Lucy minted, through keyring, for this person and this audience. So the headers are built
from scratch on every call and a small set of names is dropped on the way out
(:data:`NEVER_FORWARDED`) -- not because a pack is expected to set ``Authorization``, but
because the day one does by accident is the day the hub becomes a confused deputy, and a
header that never leaves is cheaper than noticing. Dropping rather than refusing is
deliberate: a defence in depth must never be the thing that fails somebody's turn.

**A 401 is re-minted once and retried.** Tokens are minutes long by design, so one expiring
mid-turn is ordinary rather than exceptional, and a person should never see it. The retry
lives in :meth:`~lucy_api.auth.broker.TokenBroker.attempt`, which does it exactly once
because it is written without a loop.

The errors here are three, not the nine of the plan's vocabulary. This layer can honestly
tell apart "your credential", "your request" and "their service"; it cannot tell what a
404 *means*, because that depends on what the pack asked for -- an absent playlist is a
normal answer and an absent endpoint is a deployment bug. So the status travels with the
error and the pack, which knows, does the rest. A failure to *mint* is none of the three
and is deliberately not caught here: it means Lucy cannot act for this person at all,
which is a fact about the turn rather than about one sibling, so it travels as itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from lucy_api.packs.context import Call, Http

DEFAULT_TIMEOUT_SECONDS = 10.0

NEVER_FORWARDED = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "x-keyring-user-token",
        "x-settings-user-token",
    }
)
"""Header names stripped from every outbound call, whatever a pack put in them.

The family's two user-token headers are here beside ``Authorization`` because they are the
plausible mistake: a pack written by copying an inbound request forwards what it copied.
"""

UNREACHABLE = "the service could not be reached"
MALFORMED = "the service answered with something that is not JSON"
REFUSED = "the service refused a freshly minted token"


class Broker(Protocol):
    """The broker, as this module needs it: run one attempt, survive one stale token."""

    async def attempt[T](
        self,
        audience: str,
        send: Callable[[str], Awaitable[T]],
        *,
        refused: type[Exception],
    ) -> T: ...


class DownstreamError(Exception):
    """A call to a sibling did not produce an answer the caller can use.

    Carries the status, the audience it was for, and whatever the service said, because a
    capability turning this into a sentence for the model needs all three, and because
    "which sibling" is the first question anybody debugging one asks.
    """

    def __init__(
        self,
        detail: str,
        *,
        audience: str,
        status: int = 0,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.audience = audience
        self.status = status
        self.retry_after = retry_after


class DownstreamRefusedError(DownstreamError):
    """A sibling refused a token minted seconds earlier, twice.

    Not an expiry: that is what the retry is for. This is a real disagreement -- an
    audience the sibling does not answer to, or keys the two of them no longer share --
    and it is the operator's, not the person's.
    """


class DownstreamRejectedError(DownstreamError):
    """The sibling answered and refused the request itself.

    Everything from 403 to 429. The status is what distinguishes them and the pack is what
    knows the difference, so this does not pretend to.
    """


class DownstreamUnavailableError(DownstreamError):
    """The sibling is unreachable, unwell, or answered with something unreadable."""


class _CredentialRefusedError(Exception):
    """A 401 came back. Private, so nothing outside this module can trigger the re-mint."""


class NullHttp:
    """An HTTP seam that refuses every call.

    Help, and any other pack that never leaves the process, still need *an* ``Http`` on the
    context because the type does not make the field optional. A missing client that raised
    only at the first real outbound call would hide a wiring mistake until somebody asked
    about music. Refusing here, with the audience named, makes that mistake a failed probe
    instead of a 401 against the wrong host.
    """

    async def request(self, call: Call) -> Any:
        raise DownstreamUnavailableError(UNREACHABLE, audience=call.audience)

    async def request_response(self, call: Call) -> Any:
        raise DownstreamUnavailableError(UNREACHABLE, audience=call.audience)


class PackHttp:
    """The :class:`~lucy_api.packs.context.Http` implementation capabilities are handed.

    Args:
        tokens: the broker that mints for one person. See :class:`Broker`.
        timeout_seconds: per-request timeout. A sibling that hangs must not hang a turn.
        transport: an httpx transport, so the whole pack layer runs offline in tests.
    """

    def __init__(
        self,
        *,
        tokens: Broker,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._tokens = tokens
        # Redirects are off: this client sends a credential, and a redirect is somebody
        # else's server asking for it. A shared client belongs to the composition root
        # and is not closed here, so a per-request broker does not tear down the pool.
        self._owns_http = client is None
        self._http = client or httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport, follow_redirects=False
        )

    async def request(self, call: Call) -> Any:
        """Make one authorized call and return its decoded body, or ``None`` for no body.

        Raises:
            DownstreamRefusedError: refused twice, with a fresh token the second time.
            DownstreamRejectedError: the sibling refused the request; ``status`` says how.
            DownstreamUnavailableError: it is down, or it answered unintelligibly.
        """
        response = await self.request_response(call)
        _raise_for(response, call.audience)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise DownstreamUnavailableError(
                MALFORMED, audience=call.audience, status=response.status_code
            ) from exc

    async def request_response(self, call: Call) -> httpx.Response:
        """Authenticate once and preserve status and headers for sibling adapters."""
        try:
            return await self._tokens.attempt(
                call.audience,
                lambda token: self._send(call, token),
                refused=_CredentialRefusedError,
            )
        except _CredentialRefusedError as exc:
            raise DownstreamRefusedError(
                REFUSED, audience=call.audience, status=httpx.codes.UNAUTHORIZED
            ) from exc

    async def aclose(self) -> None:
        """Release the connection pool, when this object created it."""
        if self._owns_http:
            await self._http.aclose()

    async def _send(self, call: Call, token: str) -> httpx.Response:
        """One attempt, with one token, saying only whether that token was the problem."""
        try:
            response = await self._http.request(
                call.method,
                call.url,
                json=call.json,
                params=dict(call.params) if call.params is not None else None,
                headers=_outbound(call.headers, token),
            )
        except httpx.HTTPError as exc:
            raise DownstreamUnavailableError(UNREACHABLE, audience=call.audience) from exc
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise _CredentialRefusedError
        return response


def _outbound(headers: Mapping[str, str] | None, token: str) -> dict[str, str]:
    """Build the outgoing headers, dropping anything that could carry another authority."""
    safe = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() not in NEVER_FORWARDED
    }
    safe["Authorization"] = f"Bearer {token}"
    return safe


def _raise_for(response: httpx.Response, audience: str) -> None:
    """Classify a refusal by who can do something about it, and no further."""
    status = response.status_code
    if status < httpx.codes.BAD_REQUEST:
        return
    detail = _detail_of(response)
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise DownstreamUnavailableError(detail, audience=audience, status=status)
    raise DownstreamRejectedError(
        detail, audience=audience, status=status, retry_after=_retry_after(response)
    )


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds the sibling asked us to wait, and ``None`` when it did not say.

    ``None`` for an HTTP-date too. Obeying ``Retry-After`` means obeying the number that
    was sent; converting a date against a clock that may disagree with the sender's is
    inventing an interval, which is the one thing a caller must not do.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _detail_of(response: httpx.Response) -> str:
    """The ``detail`` out of a problem+json body, or the first of whatever came instead."""
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else response.text[:200]


if TYPE_CHECKING:

    def _pack_http_is_the_http_seam(client: PackHttp) -> Http:
        """Checked by mypy and never run: this is what ``PackContext.http`` holds."""
        return client


__all__ = [
    "NEVER_FORWARDED",
    "Broker",
    "DownstreamError",
    "DownstreamRefusedError",
    "DownstreamRejectedError",
    "DownstreamUnavailableError",
    "NullHttp",
    "PackHttp",
]
