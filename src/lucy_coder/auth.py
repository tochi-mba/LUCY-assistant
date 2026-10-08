"""Believe a keyring token for the ``coder-api`` audience, or refuse it.

The one thing this service trusts. The hub mints a token per call for exactly this
audience; anything else -- another sibling's token, the person's own session token, nothing
at all -- reads as 401. Keyring being unreachable is 503, never a pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from keyring_client import AuthenticationError as TokenRefusedError
from keyring_client import ExactAudience
from keyring_client import KeyringUnreachableError as KeysUnavailableError
from keyring_client import TokenVerifier as KeyringTokenVerifier

if TYPE_CHECKING:
    from keyring_client import Clock, JwksClient


@dataclass(frozen=True, slots=True)
class VerifiedCaller:
    """Who a verified token says is asking."""

    account_id: str
    audience: str


class AuthenticationError(Exception):
    """The bearer token is missing or refused."""


class KeyringUnreachableError(Exception):
    """Keyring's signing keys could not be fetched."""


TOKEN_REFUSED = "token refused"  # noqa: S105 - error message, not a credential
KEYS_UNAVAILABLE = "keyring keys unavailable"


class TokenVerifier:
    """Turns a bearer string into a :class:`VerifiedCaller`, or refuses it."""

    def __init__(self, *, jwks: JwksClient, issuer: str, audience: str, clock: Clock) -> None:
        self._verifier = KeyringTokenVerifier(jwks=jwks, issuer=issuer, clock=clock)
        self._audience = ExactAudience(audience)

    async def verify(self, token: str) -> VerifiedCaller:
        """Verify ``token`` for this service's audience."""
        try:
            verified = await self._verifier.verify(token, audience=self._audience)
        except TokenRefusedError as exc:
            raise AuthenticationError(TOKEN_REFUSED) from exc
        except KeysUnavailableError as exc:
            raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc
        return VerifiedCaller(account_id=verified.account_id, audience=verified.audience)
