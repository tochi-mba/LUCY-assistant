"""One way to sign a body and check a signature, for everything Lucy sends or receives.

Lucy signs the webhooks it sends, and checks the signals siblings send it when a
subscription ends. Both are the same scheme -- `X-Lucy-Signature: sha256=<hex HMAC of the raw
body>` -- so a sibling that verifies one can produce the other, and there is one place where
the comparison is done in constant time rather than two places where one of them is not.

The family's signal client (`lucy_signals`, beside this hub under `clients/python`) speaks the
same scheme, and a contract test holds the two to the same bytes.
"""

from __future__ import annotations

import hashlib
import hmac

HEADER = "X-Lucy-Signature"
"""Where the signature travels. The name a sibling reads in Lucy's docs, and the one it sends."""

PREFIX = "sha256="


def sign(secret: str, body: bytes) -> str:
    """The header value for `body` under `secret`."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return PREFIX + digest


def verify(secret: str, header: str | None, body: bytes) -> bool:
    """Whether `header` is `body` signed with `secret`.

    Constant time over the whole value, prefix included: a comparison that stopped at the
    first wrong character would say, in its timing, how much of a forged signature was right.
    A missing or malformed header is simply not a match.
    """
    if not header:
        return False
    return hmac.compare_digest(header.encode("utf-8"), sign(secret, body).encode("utf-8"))


__all__ = ["HEADER", "PREFIX", "sign", "verify"]
