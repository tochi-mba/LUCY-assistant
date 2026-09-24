"""The one way a capability reaches a sibling, and the one place a token is attached.

Capabilities never build their own HTTP client and never see a credential. They describe a
call -- method, url, audience -- and this attaches authority for that audience and nothing
else. Concentrating it here is what makes the hub's central promise checkable in one file
rather than in every pack somebody writes afterwards.

**A caller's token is never forwarded.** Every outbound request is authorized with a token
Lucy minted, through keyring, for this person and this audience. A sibling that speaks the
two-credential internal contract (Memory-api today) takes Lucy's own service token as
``Authorization`` and that minted token as ``X-Keyring-User-Token``; every other sibling
takes the minted token as Bearer. The headers are built from scratch on every call and a
small set of names is dropped on the way out (:data:`NEVER_FORWARDED`) -- not because a pack
is expected to set ``Authorization``, but because the day one does by accident is the day
the hub becomes a confused deputy, and a header that never leaves is cheaper than noticing.
Dropping rather than refusing is deliberate: a defence in depth must never be the thing that
fails somebody's turn.

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

import asyncio
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from lucy_api.packs.context import Call, Http

DEFAULT_TIMEOUT_SECONDS = 10.0

SUBJECT_HEADER = "X-Keyring-User-Token"
"""Where a minted person token travels when ``Authorization`` is Lucy's own service token.

Memory-api's internal surface (and Keyring's) take two credentials: the calling service in
``Authorization``, the person as subject proof in this header. PackHttp is the only place
that header is set, so a pack that copies an inbound request cannot put the caller's JWT
there — :data:`NEVER_FORWARDED` strips it first.
"""

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

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
"""The methods RFC 9110 section 9.2.2 lets a client send twice for the effect of once.

A timeout after sending, a dropped connection mid-answer, or a 5xx is retried only for
these. For a POST each of those can mean the sibling already did the work and was slow to
say so: a ``workspace.run`` whose answer outlasted the call timeout was sent again and the
command ran twice, and Spotify-api's own 504 -- "Spotify accepted the command but its
effect could not be confirmed" -- was answered by POSTing play again, which restarted the
track that was already playing.
"""

NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
"""Transport failures that happen before a single byte of the request leaves.

The one kind of failure a POST can be retried after, because the sibling cannot have acted
on a request it never received. A read timeout is deliberately not here: by then the whole
request was written, and the sibling may be halfway through it.
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
        service_tokens: Lucy's own credential per sibling audience that speaks the
            two-credential internal contract. Empty means every call uses a minted Bearer.
            Values never appear in :meth:`__repr__`.
    """

    def __init__(  # noqa: PLR0913 - clock and sleeper exist so retries are asserted, not waited
        self,
        *,
        tokens: Broker,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
        service_tokens: Mapping[str, str] | None = None,
        retry_attempts: int = 0,
        retry_max_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._tokens = tokens
        # Redirects are off: this client sends a credential, and a redirect is somebody
        # else's server asking for it. A shared client belongs to the composition root
        # and is not closed here, so a per-request broker does not tear down the pool.
        self._owns_http = client is None
        self._timeout_seconds = timeout_seconds
        self.retry_attempts = retry_attempts
        self.retry_max_seconds = retry_max_seconds
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._http = client or httpx.AsyncClient(
            timeout=timeout_seconds, transport=transport, follow_redirects=False
        )
        self._service_tokens = {
            audience: secret.strip()
            for audience, secret in (service_tokens or {}).items()
            if secret.strip()
        }

    def apply_policy(self, policy: object) -> None:
        """Copy this turn's timeout and retry budget onto an already-built client.

        Connection and environment clients keep the deployment timeout. Only
        ``prepare_turn`` calls this, so a settings outage cannot silently widen retries.
        """
        timeout = getattr(policy, "downstream_timeout_seconds", None)
        if isinstance(timeout, int | float) and not isinstance(timeout, bool) and timeout > 0:
            self._timeout_seconds = float(timeout)
        attempts = getattr(policy, "retry_attempts", None)
        if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0:
            self.retry_attempts = attempts
        window = getattr(policy, "retry_max_seconds", None)
        if isinstance(window, int | float) and not isinstance(window, bool) and window > 0:
            self.retry_max_seconds = float(window)

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
        """Authenticate once and preserve status and headers for sibling adapters.

        A failure is retried only when sending the request again cannot do its work twice:
        any method that never left (:data:`NEVER_SENT`) or was turned away with a 429, and
        otherwise only :data:`IDEMPOTENT_METHODS` and a call that says it is ``repeatable``.
        """
        deadline = self._clock() + self.retry_max_seconds
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._tokens.attempt(
                    call.audience,
                    lambda token: self._send(call, token),
                    refused=_CredentialRefusedError,
                )
            except _CredentialRefusedError as exc:
                raise DownstreamRefusedError(
                    REFUSED, audience=call.audience, status=httpx.codes.UNAUTHORIZED
                ) from exc
            except DownstreamUnavailableError as exc:
                unsent = isinstance(exc.__cause__, NEVER_SENT)
                if not (unsent or _idempotent(call)) or not self._may_retry(attempt, deadline):
                    raise
                await self._sleeper(0.0)
                continue
            if _retryable(call, response.status_code) and self._may_retry(attempt, deadline):
                await self._sleeper(_backoff(_retry_after(response), self.retry_max_seconds))
                continue
            return response

    def _may_retry(self, attempt: int, deadline: float) -> bool:
        return attempt <= self.retry_attempts and self._clock() < deadline

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
                headers=_outbound(
                    call.headers, token, service_token=self._service_tokens.get(call.audience, "")
                ),
                timeout=call.timeout_seconds or self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DownstreamUnavailableError(UNREACHABLE, audience=call.audience) from exc
        if response.status_code == httpx.codes.UNAUTHORIZED:
            raise _CredentialRefusedError
        return response


def apply_downstream_policy(http: object, policy: object) -> None:
    """Reach through GuardedHttp to the PackHttp a turn actually uses."""
    target = getattr(http, "inner", http)
    apply = getattr(target, "apply_policy", None)
    if callable(apply):
        apply(policy)


def _idempotent(call: Call) -> bool:
    if call.repeatable is not None:
        return call.repeatable
    return call.method.upper() in IDEMPOTENT_METHODS


def _retryable(call: Call, status: int) -> bool:
    """A 429 says the request was not processed; a 5xx says nothing about whether it was."""
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return True
    return status >= httpx.codes.INTERNAL_SERVER_ERROR and _idempotent(call)


def _backoff(retry_after: float | None, ceiling: float) -> float:
    if retry_after is None or retry_after < 0:
        return 0.0
    return min(retry_after, ceiling)


def _outbound(
    headers: Mapping[str, str] | None, token: str, *, service_token: str = ""
) -> dict[str, str]:
    """Build the outgoing headers, dropping anything that could carry another authority.

    A sibling on the two-credential contract takes Lucy's service token as Bearer and the
    minted person token as subject proof. Every other sibling takes the minted token as
    Bearer. Either way the inbound caller token has already been stripped.
    """
    safe = {
        name: value
        for name, value in (headers or {}).items()
        if name.lower() not in NEVER_FORWARDED
    }
    if service_token:
        safe["Authorization"] = f"Bearer {service_token}"
        safe[SUBJECT_HEADER] = token
        return safe
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
    "SUBJECT_HEADER",
    "Broker",
    "DownstreamError",
    "DownstreamRefusedError",
    "DownstreamRejectedError",
    "DownstreamUnavailableError",
    "NullHttp",
    "PackHttp",
    "apply_downstream_policy",
]
