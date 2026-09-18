"""One vocabulary for every way a sibling can say no.

Nine services, nine sets of failure modes, and a hub that has to behave the same way
whichever one produced them. So the status code is translated once, here, into a named
exception that says what the *caller* should do about it -- and the callers are packs,
which have exactly one decision to make per kind: retry, re-mint, offer setup, tell the
person, or surface a state.

The distinctions that earn their place:

**A 404 is absence, not failure.** A memory another account owns and a memory that never
existed answer identically, by design, in every service in this family. A client that told
them apart would be describing a service that leaks.

**A 409 is a state.** A cap that is full or a value the operator pinned is not a thing that
went wrong; it is a fact about the account, and it reaches a person as "you are at your
limit", not as an error. It is raised because the call did not happen, and named so that a
pack renders it as a state rather than apologising for it.

**A 502 naming a missing credential means not connected.** That is the one translation the
whole connection story rests on: it flips a probe from `ready` to `not_connected`, which
turns an apology into a link the person can click. Nothing else in this module changes what
a capability *is*; this one does.

**A 429 carries the interval the service gave, or nothing.** `Retry-After` is obeyed and
never invented. A client that guesses an interval is a client that turns one service's rate
limit into a thundering herd against it.

Two things are deliberately absent. There is no retry here: re-minting a token after a 401
belongs to the thing that holds the token broker, which is the HTTP seam beneath this, and
a client that retried as well would double every backoff. And nothing here is written for a
model to read. `service` is an internal name and `detail` is another service's prose; a
pack translates both into product words before anything reaches a prompt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    class Response(Protocol):
        """What a client needs back from the HTTP seam.

        `Http.request` is annotated `Any` so that the capability contract does not name a
        transport library, which leaves the shape to be agreed here. It is the response
        rather than the decoded body, because every rule above needs the status, and two
        of them need a header: without them a 429 cannot be obeyed and a missing
        credential cannot be told from an outage.
        """

        @property
        def status_code(self) -> int: ...

        @property
        def headers(self) -> Mapping[str, str]: ...

        def json(self) -> Any: ...


CREDENTIAL_CODES = frozenset({"credential-unavailable", "credential-missing"})
"""Problem codes that mean "no credential", in both spellings the family uses.

Spotify-api derives its problem type from `credential_unavailable`; another credential
consumer may report `credential_missing`. Underscores and hyphens are folded together
before the comparison, because which one a service used is an accident of how it renders a
slug.
"""

RETRY_AFTER = "Retry-After"


class DownstreamError(Exception):
    """A sibling answered, and the answer was no.

    Carries the status and the service's own `detail` because the interesting cases are all
    ones somebody can act on, and "invalid input" helps nobody. The message is for a log or
    a pack, never for a prompt.
    """

    def __init__(self, service: str, status: int, detail: str = "", code: str = "") -> None:
        self.service = service
        self.status = status
        self.detail = detail
        self.code = code
        said = f": {detail}" if detail else ""
        super().__init__(f"{service} answered {status}{said}")


class ReauthenticationError(DownstreamError):
    """The token was refused. Mint a fresh one for this audience and try once more.

    Reaching a pack means the retry beneath already happened and failed, so this is an
    operator problem -- a misconfigured audience, a clock skew -- and not something to
    retry again in a loop.
    """


class ForbiddenError(DownstreamError):
    """A fact about this token: it does not grant what was asked for.

    Never retried with a different scope. Widening a request until it succeeds is how a
    confused deputy is built, and the scope a token carries is the person's decision.
    """


class AbsentError(DownstreamError):
    """No such thing, or none this person may see. The two are one answer on purpose."""


class ConflictError(DownstreamError):
    """A cap is full or a value is pinned: a state, not a failure.

    Raised because the call did not happen, named so that whoever catches it says "you are
    at your limit" rather than "something went wrong".
    """


class PreconditionError(DownstreamError):
    """It changed under you. Re-read it and reapply the change to what is there now."""


class RejectedError(DownstreamError):
    """The value cannot be stored as sent, and the detail says what would be accepted.

    A credential is the common case, and the fix is always the same one: it belongs in the
    vault, and what goes here is a description of it.
    """


class RateLimitedError(DownstreamError):
    """Too many requests. `retry_after` is the service's interval, or `None`.

    `None` means the service did not say. It does not mean zero, and it must not become a
    number this client made up.
    """

    def __init__(
        self,
        service: str,
        status: int,
        detail: str = "",
        code: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(service, status, detail, code)
        self.retry_after = retry_after


class NotConnectedError(DownstreamError):
    """No usable credential for this person and profile. The capability is not connected.

    The one failure here that is not a failure: it is what a probe reads to move a
    capability into `not_connected`, so the person is offered a link instead of an apology.
    """


class UnavailableError(DownstreamError):
    """The service is down or broken. Keep the capability, retry it, notice it on the run."""


def problem_code(body: Any) -> str:
    """The stable discriminator out of a problem document, folded to one spelling.

    Three shapes are read because three are in use: RFC 9457's `type`, whose last segment
    every service in the family keeps stable, a flat `code`, and an `error` object with one
    inside. Anything else yields an empty string, which is a code that matches nothing
    rather than a decode that fails.
    """
    if not isinstance(body, dict):
        return ""
    nested = body.get("error")
    inner = nested.get("code") if isinstance(nested, dict) else None
    raw = body.get("code") or inner or str(body.get("type", "")).rsplit("/", maxsplit=1)[-1]
    return str(raw).replace("_", "-").strip().lower()


def problem_detail(body: Any) -> str:
    """The sentence a service wrote about this failure, if it wrote one."""
    if not isinstance(body, dict):
        return ""
    nested = body.get("error")
    if isinstance(nested, dict) and nested.get("message"):
        return str(nested["message"])
    return str(body.get("detail") or body.get("title") or "")


def retry_after(headers: Mapping[str, str]) -> float | None:
    """`Retry-After` in seconds, in either form RFC 9110 allows, or `None` if absent.

    The date form is resolved against the current clock, which is the only thing it can
    mean. A header this client cannot parse becomes `None` rather than a default: a made-up
    interval is worse than no interval, because the caller that receives one stops thinking.
    """
    raw = headers.get(RETRY_AFTER) or headers.get(RETRY_AFTER.lower())
    if not raw:
        return None
    try:
        return float(int(raw))
    except ValueError:
        return _seconds_until(raw)


def _seconds_until(raw: str) -> float | None:
    """Seconds from now to an HTTP-date, never negative, or `None` if it is not one."""
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # `parsedate_to_datetime` returns a naive datetime for the "-0000" zone, which RFC 9110
    # says to read as UTC. Comparing that to an aware `now` raises, so it is made aware here
    # rather than at the four places that would otherwise have to remember.
    fixed = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return max(0.0, (fixed - datetime.now(UTC)).total_seconds())


def _body_of(response: Response) -> Any:
    """The decoded body, or `None` when there is not one.

    A failing service is exactly the service most likely to answer with an empty body, an
    HTML error page from a proxy in front of it, or a truncated document. None of those may
    turn a 503 into a decode error, because the caller's whole decision rests on the status.
    """
    try:
        decoded = response.json()
    except Exception:
        # Any decoder failure means the same thing here: there is no document to read.
        return None
    return decoded


# One status, one translation, so the mapping is a table rather than a ladder of ifs.
_BY_STATUS: Mapping[int, type[DownstreamError]] = {
    401: ReauthenticationError,
    403: ForbiddenError,
    404: AbsentError,
    409: ConflictError,
    412: PreconditionError,
    422: RejectedError,
}

CLIENT_ERROR = 400
SERVER_ERROR = 500
BAD_GATEWAY = 502
TOO_MANY = 429


def raise_for(response: Response, *, service: str) -> None:
    """Translate one non-2xx answer into the vocabulary above. A 2xx returns quietly.

    The ordering is deliberate: the named statuses first, then the credential case, then the
    two fallbacks. A 4xx nobody named is a request this hub got wrong and is reported as
    such; a 5xx nobody named is an outage. Neither is allowed to become a silent success.
    """
    status = response.status_code
    if status < CLIENT_ERROR:
        return

    body = _body_of(response)
    detail, code = problem_detail(body), problem_code(body)
    named = _BY_STATUS.get(status)
    if named is not None:
        raise named(service, status, detail, code)
    if status == TOO_MANY:
        raise RateLimitedError(
            service, status, detail, code, retry_after=retry_after(response.headers)
        )
    if status == BAD_GATEWAY and code in CREDENTIAL_CODES:
        raise NotConnectedError(service, status, detail, code)
    if status >= SERVER_ERROR:
        raise UnavailableError(service, status, detail, code)
    raise RejectedError(service, status, detail, code)


__all__ = [
    "CREDENTIAL_CODES",
    "AbsentError",
    "ConflictError",
    "DownstreamError",
    "ForbiddenError",
    "NotConnectedError",
    "PreconditionError",
    "RateLimitedError",
    "ReauthenticationError",
    "RejectedError",
    "UnavailableError",
    "problem_code",
    "problem_detail",
    "raise_for",
    "retry_after",
]
