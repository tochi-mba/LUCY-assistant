"""The half of a client that is the same in all nine of them.

Every client below this module does the same four things: build a `Call` for one audience,
hand it to the HTTP seam, translate the answer into the error vocabulary, and decode a
foreign document into a narrow type of our own. Writing that nine times would mean nine
chances to forget the translation, and the one that forgot would be the one that let a raw
payload through.

## Decoding is deliberately forgiving

The readers here answer with a default rather than raising when a field is missing or the
wrong shape. That is not laziness about types, it is what a version skew across nine
separately released services actually looks like: a sibling that stops sending a field it
never promised should cost that field, not the whole turn. Anything Lucy genuinely cannot
proceed without is checked by the client that needs it, where the error can say what to do.

The same forgiveness is what makes the projections safe. A projection that only ever *takes*
named fields cannot accidentally carry a new one a sibling started sending, which is exactly
how a raw payload gets into a context window: not deliberately, but because somebody
serialised a whole response and the response grew.

## One `Sibling` rather than a base class

Composition, so a client is a plain object holding a `Sibling` rather than an inheritance
chain a reader has to walk to find out what `self.send` does. It also keeps the audience
next to the base URL, which is the pairing the whole delegation story rests on: a call to
the wrong audience with the right URL is a token somebody else can replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from lucy_api.clients.errors import raise_for
from lucy_api.context.types import Trust
from lucy_api.packs.context import Call

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucy_api.packs.context import Http

PROFILE_HEADER = "X-Keyring-Profile"
"""Which stored credential set a service should use. Never a credential itself."""

NO_CONTENT = 204


@dataclass(frozen=True, slots=True)
class Sibling:
    """Where one service lives, what its tokens are called, and what to call it in a log.

    `service` and `audience` are both here and are not the same string: `user-api` serves
    several audiences (`user.home`, `user.health`), one scope each, and a hub that assumed
    one token per service would read a fraction of a person's record and never notice.
    """

    http: Http
    base_url: str
    service: str
    audience: str

    async def send(  # noqa: PLR0913 - one request, described: verb, path, body, query, who
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        profile: str = "",
        timeout_seconds: float | None = None,
    ) -> Any:
        """One call, translated. Returns the decoded body, or `None` for a 204.

        Raises:
            DownstreamError: whatever the status meant, in the vocabulary of
                `lucy_api.clients.errors`.
        """
        call = Call(
            method=method,
            url=f"{self.base_url.rstrip('/')}{path}",
            audience=self.audience,
            json=body,
            params=params,
            headers={PROFILE_HEADER: profile} if profile else None,
            timeout_seconds=timeout_seconds,
        )
        response = await self.http.request_response(call)
        raise_for(response, service=self.service)
        if response.status_code == NO_CONTENT:
            return None
        return response.json()


def segment(value: str) -> str:
    """One path segment, escaped.

    Profile names, ids and labels all arrive from somewhere else, and a value carrying a
    slash would otherwise address a different route than the one written here. Cheap
    insurance against a bug that would read as an authorisation failure.
    """
    return quote(value, safe="")


def given(**pairs: Any) -> dict[str, Any]:
    """The pairs that were actually given, for a query string or a sparse body.

    `None` means "not asked for" and must not be sent: a service that distinguishes an
    omitted field from an explicit null -- and several here do -- would read the second as
    "clear this".
    """
    return {key: value for key, value in pairs.items() if value is not None}


def field(payload: Any, key: str) -> Any:
    """One value out of a document that may not be a document at all."""
    return payload.get(key) if isinstance(payload, dict) else None


def text(payload: Any, key: str, default: str = "") -> str:
    """A string field, with a missing one reading as absent rather than as `"None"`."""
    value = field(payload, key)
    return default if value is None else str(value)


def number(payload: Any, key: str, default: int = 0) -> int:
    """A whole-number field. Anything that will not convert is the default."""
    try:
        return int(field(payload, key))
    except (TypeError, ValueError):
        return default


def flag(payload: Any, key: str, *, default: bool = False) -> bool:
    """A boolean field. Absent is the default, which is not always `False`."""
    value = field(payload, key)
    return default if value is None else bool(value)


def rows(payload: Any, key: str) -> tuple[Any, ...]:
    """A list field, as a tuple. A missing or malformed list is no rows, never `None`."""
    value = field(payload, key)
    return tuple(value) if isinstance(value, list) else ()


def nested(payload: Any, key: str) -> Any:
    """A nested object, or an empty one, so a chain of reads never has to check for null."""
    value = field(payload, key)
    return value if isinstance(value, dict) else {}


def moment(value: Any) -> datetime | None:
    """A timestamp in either shape the family uses, or `None` if it is neither.

    Epoch seconds from the services that store floats, ISO 8601 from the ones that store
    strings, and UTC assumed for a string that names no zone -- which is what every service
    here means by a bare timestamp, and is better than a naive datetime that raises the
    first time somebody subtracts it from an aware one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def trust_from(value: Any, known: Mapping[str, Trust]) -> Trust:
    """One service's provenance word, read as a trust level, failing closed.

    Each service names provenance in its own vocabulary, so the table is the caller's. What
    is not the caller's is the default: a word this hub does not recognise -- a level from a
    newer sibling, a typo, a string an attacker chose -- becomes `untrusted`, which asks a
    person before it reaches the model. Defaulting the other way would hand the index to
    whoever controls the payload.
    """
    return known.get(str(value), Trust.untrusted)


def capped[T](items: Sequence[T], limit: int, noun: str) -> tuple[tuple[T, ...], str]:
    """The first `limit` items and the confession, which is empty when nothing was cut.

    Nothing truncates silently, so the counts are exact and the caller has no way to keep
    the shortened list without the sentence that says it is shortened.
    """
    if len(items) <= limit:
        return tuple(items), ""
    return tuple(items[:limit]), f"showing {limit} of {len(items)} {noun}"


__all__ = [
    "NO_CONTENT",
    "PROFILE_HEADER",
    "Sibling",
    "capped",
    "field",
    "flag",
    "given",
    "moment",
    "nested",
    "number",
    "rows",
    "segment",
    "text",
    "trust_from",
]
