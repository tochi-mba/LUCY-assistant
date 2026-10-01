"""Sign, send and verify the signal that ends a Lucy subscription.

The contract is Lucy's (`docs/jobs.md` in LUCY-assistant), and the hub holds its own copy of
the scheme in `lucy_api.net.signing`; a contract test there keeps the two byte-identical.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    import httpx

__version__ = "0.1.0"

HEADER = "X-Lucy-Signature"
PREFIX = "sha256="

MAX_SUMMARY = 120
MAX_EXCERPT = 1_500

RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 30.0, 90.0)
"""Three more tries after the first, over about two minutes. Lucy's sweep covers the rest."""

DELIVERED = frozenset({204, 409})
"""204: the subscription ended. 409: it had already ended -- an earlier try was heard."""

SERVER_ERROR = 500
"""From here up the failure is Lucy's, and worth another try; below it, it is the request's."""

Outcome = Literal["delivered", "refused", "unreachable"]


@dataclass(frozen=True, slots=True)
class Signal:
    """That the condition held, or did not; never the result itself."""

    state: Literal["fired", "failed", "expired"]
    summary: str
    facts: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)
    excerpt: str = ""

    def body(self) -> bytes:
        """The exact bytes that are signed and sent. Summary and excerpt are bounded."""
        document: dict[str, object] = {
            "state": self.state,
            "summary": " ".join(self.summary.split())[:MAX_SUMMARY],
        }
        if self.facts:
            document["facts"] = dict(self.facts)
        if self.excerpt:
            document["excerpt"] = self.excerpt[:MAX_EXCERPT]
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret: str, body: bytes) -> str:
    """The `X-Lucy-Signature` value for `body` under `secret`."""
    return PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, header: str | None, body: bytes) -> bool:
    """Whether `header` signs `body` with `secret`, compared in constant time."""
    if not header:
        return False
    return hmac.compare_digest(header.encode("utf-8"), sign(secret, body).encode("utf-8"))


async def deliver(
    client: httpx.AsyncClient,
    *,
    url: str,
    secret: str,
    signal: Signal,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Outcome:
    """Send one signal, retrying what is worth retrying. Never raises for a refusal.

    Returns ``delivered`` for 204 or 409, ``refused`` for any other 4xx (a retry would be
    refused the same way: the subscription is unknown or the secret is wrong), and
    ``unreachable`` when every try failed to connect or met a 5xx.
    """
    import httpx  # noqa: PLC0415 - a verifier-only consumer need not import the network stack

    body = signal.body()
    headers = {HEADER: sign(secret, body), "content-type": "application/json"}
    for delay in (0.0, *RETRY_DELAYS_SECONDS):
        if delay:
            await sleep(delay)
        try:
            response = await client.post(url, content=body, headers=headers)
        except httpx.HTTPError:
            continue
        if response.status_code in DELIVERED:
            return "delivered"
        if response.status_code < SERVER_ERROR:
            return "refused"
    return "unreachable"


__all__ = [
    "DELIVERED",
    "HEADER",
    "PREFIX",
    "RETRY_DELAYS_SECONDS",
    "Outcome",
    "Signal",
    "__version__",
    "deliver",
    "sign",
    "verify_signature",
]
