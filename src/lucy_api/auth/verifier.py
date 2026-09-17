"""Believe a keyring token, or refuse it.

The hub verifies locally against keyring's published keys and never asks keyring "is this
token good?" on every request. An outage of keyring therefore degrades *issuance* -- nobody
new can sign in -- rather than verification of tokens already minted, until the keys go
stale.

The two failures are deliberately different exceptions, because they mean opposite things
to the person holding the token: a refused token is their problem and answers 401, while
unfetchable keys are ours and answer 503. Telling somebody to log in again because keyring
blipped is advice that does not help.
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
    """Who a verified token says is asking. The subject is opaque and is the only key."""

    account_id: str
    audience: str


class AuthenticationError(Exception):
    """The bearer token is missing or refused."""


class KeyringUnreachableError(Exception):
    """Keyring's signing keys could not be fetched."""


TOKEN_REFUSED = "token refused"  # noqa: S105 -- an error message, not a credential
KEYS_UNAVAILABLE = "keyring keys unavailable"


class TokenVerifier:
    """Turns a bearer string into a :class:`VerifiedCaller`, or refuses it."""

    def __init__(self, *, jwks: JwksClient, issuer: str, audience: str, clock: Clock) -> None:
        self._verifier = KeyringTokenVerifier(jwks=jwks, issuer=issuer, clock=clock)
        self._audience = ExactAudience(audience)

    async def verify(self, token: str) -> VerifiedCaller:
        """Verify ``token`` for this hub's audience exactly."""
        try:
            verified = await self._verifier.verify(token, audience=self._audience)
        except TokenRefusedError as exc:
            raise AuthenticationError(TOKEN_REFUSED) from exc
        except KeysUnavailableError as exc:
            raise KeyringUnreachableError(KEYS_UNAVAILABLE) from exc
        return VerifiedCaller(account_id=verified.account_id, audience=verified.audience)
