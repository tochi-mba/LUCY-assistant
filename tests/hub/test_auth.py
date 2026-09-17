"""Who the hub believes, and how it says no.

The two refusals are different on purpose: a refused token is the caller's problem (401),
unfetchable keys are ours (503 with Retry-After). A health route proves the split without
needing an authenticated route to exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, FakeKeyring, mint

from lucy_api.api.dependencies import MISSING_CREDENTIALS
from lucy_api.api.errors import RETRY_AFTER_SECONDS, register_exception_handlers
from lucy_api.auth.verifier import (
    KEYS_UNAVAILABLE,
    TOKEN_REFUSED,
    AuthenticationError,
    KeyringUnreachableError,
    VerifiedCaller,
)
from lucy_api.core.container import build_container

if TYPE_CHECKING:
    from lucy_api.core.config import Settings

AUDIENCE = "lucy-api"


def _app_that_raises(error: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise error

    return app


@pytest.mark.asyncio
async def test_a_refused_token_is_401_and_an_unreachable_keyring_is_503() -> None:
    for error, status, detail in (
        (AuthenticationError(MISSING_CREDENTIALS), 401, MISSING_CREDENTIALS),
        (AuthenticationError(TOKEN_REFUSED), 401, TOKEN_REFUSED),
        (KeyringUnreachableError(KEYS_UNAVAILABLE), 503, KEYS_UNAVAILABLE),
    ):
        app = _app_that_raises(error)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get("/boom")
        assert response.status_code == status
        assert response.json()["detail"] == detail
        if status == 503:
            assert response.headers["Retry-After"] == RETRY_AFTER_SECONDS


@pytest.mark.asyncio
async def test_a_good_token_verifies_to_its_subject(
    settings: Settings, keyring: FakeKeyring
) -> None:
    container = build_container(settings, transport=keyring.transport())
    try:
        caller = await container.verifier.verify(
            mint(account_id="acct_a", audience=AUDIENCE, issuer=ISSUER)
        )
    finally:
        await container.aclose()
    assert caller == VerifiedCaller(account_id="acct_a", audience=AUDIENCE)


@pytest.mark.asyncio
async def test_a_token_for_another_audience_is_refused(
    settings: Settings, keyring: FakeKeyring
) -> None:
    # Exact audience, not a family: a token minted for persona must not open the hub.
    container = build_container(settings, transport=keyring.transport())
    try:
        with pytest.raises(AuthenticationError, match=TOKEN_REFUSED):
            await container.verifier.verify(
                mint(account_id="acct_a", audience="persona", issuer=ISSUER)
            )
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_unfetchable_keys_raise_the_other_error(
    settings: Settings, keyring: FakeKeyring
) -> None:
    keyring.error = httpx.ConnectError("down")
    container = build_container(settings, transport=keyring.transport())
    try:
        with pytest.raises(KeyringUnreachableError, match=KEYS_UNAVAILABLE):
            await container.verifier.verify(
                mint(account_id="acct_a", audience=AUDIENCE, issuer=ISSUER)
            )
    finally:
        await container.aclose()


def test_a_verified_caller_is_frozen() -> None:
    caller = VerifiedCaller(account_id="acct_a", audience=AUDIENCE)
    with pytest.raises(AttributeError):
        caller.account_id = "acct_b"  # type: ignore[misc]
