"""Who the hub believes, and how it says no.

The two refusals are different on purpose: a refused token is the caller's problem (401),
unfetchable keys are ours (503 with Retry-After). A health route proves the split without
needing an authenticated route to exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from conftest import build_settings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from keyring_client.testing import ISSUER, FakeKeyring, mint
from settings_client import ResolvedSettings
from settings_client.errors import SettingsClientError, SettingsRejected, SettingsUnavailable
from settings_client.testing import FakeSettingsClient

from lucy_api.api.dependencies import MISSING_CREDENTIALS
from lucy_api.api.errors import RETRY_AFTER_SECONDS, register_exception_handlers
from lucy_api.auth.verifier import (
    KEYS_UNAVAILABLE,
    TOKEN_REFUSED,
    AuthenticationError,
    KeyringUnreachableError,
    VerifiedCaller,
)
from lucy_api.core.container import PackRequest, build_container
from lucy_api.core.errors import LucyError
from lucy_api.packs.http import PackHttp
from lucy_api.packs.probes import GuardedHttp
from lucy_api.settings.policy import SETTINGS_UNAVAILABLE

if TYPE_CHECKING:
    from lucy_api.core.config import Settings

AUDIENCE = "lucy-api"


class FailingSettings(FakeSettingsClient):
    """A settings seam that distinguishes an outage from an authorization failure."""

    def __init__(self, error: SettingsClientError) -> None:
        super().__init__({})
        self.error = error

    async def resolve(
        self, namespace: str, *, user_token: str, profile: str | None = None
    ) -> ResolvedSettings:
        del profile
        raise self.error


class ProfileAwareSettings(FakeSettingsClient):
    """A client that already accepts profile, so the TypeError fallback is not taken."""

    def __init__(self) -> None:
        super().__init__({"lucy": {"max_llm_turns": 9}})
        self.profiles: list[str | None] = []

    async def resolve(
        self, namespace: str, *, user_token: str, profile: str | None = None
    ) -> ResolvedSettings:
        self.profiles.append(profile)
        return await super().resolve(namespace, user_token=user_token)


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


@pytest.mark.asyncio
async def test_a_service_token_lets_packs_mint_instead_of_forwarding(
    keyring: FakeKeyring,
) -> None:
    """Lucy acts with a minted token. An empty service token is a NullHttp seam, not a hang."""
    settings = build_settings(keyring_service_token="s" * 32)
    container = build_container(settings, transport=keyring.transport())
    try:
        context = container.pack_context(
            PackRequest(
                caller=VerifiedCaller(account_id="acct_a", audience=AUDIENCE),
                user_token="a.verified.jwt",
                profile="personal",
                session_id="ses_a",
            )
        )
        assert isinstance(context.http, GuardedHttp)
        assert isinstance(context.http.inner, PackHttp)
        assert container.uptime_seconds >= 0
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_turn_limits_are_resolved_once_with_the_live_feed_policy(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    preferences = FakeSettingsClient(
        {
            "lucy": {
                "max_llm_turns": 21,
                "max_subagent_turns": 13,
                "max_tool_calls_per_turn": 44,
                "max_turn_seconds": 300,
                "feeds_music": False,
            }
        }
    )
    await container.preferences.aclose()
    container.preferences = preferences
    try:
        await container.start()
        prepared = await container.prepare_turn(
            PackRequest(
                caller=VerifiedCaller(account_id="acct_a", audience=AUDIENCE),
                user_token="a.verified.jwt",
                profile="personal",
                session_id="ses_a",
            ),
            {"profile": "personal"},
        )
        assert prepared.budget.max_iterations == 21
        assert prepared.budget.max_tool_calls == 44
        assert prepared.budget.max_seconds == 300
        assert prepared.max_subagent_turns == 13
        assert prepared.pack_context.max_subagent_turns == 13
        assert prepared.pack_context.policy.max_llm_turns == 21
        assert prepared.pack_context.policy.max_subagent_turns == 13
        assert prepared.pack_context.policy.max_tool_calls == 44
        assert prepared.live.flags is not None
        assert prepared.live.flags.flag("feeds_music") is False
        # One resolve per namespace the turn reads: lucy, then the two sibling namespaces
        # whose knobs the packs may use when the model omits them. Never one per knob.
        assert preferences.resolves == 3
        assert prepared.live.sources is not None
        assert prepared.live.sources.topics is not None
        assert prepared.live.sources.in_flight is not None
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_a_profile_aware_settings_client_receives_the_session_profile(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    preferences = ProfileAwareSettings()
    await container.preferences.aclose()
    container.preferences = preferences
    try:
        resolved = await container._turn_settings("a.verified.jwt", "work")
        assert resolved is not None
        assert resolved.get("max_llm_turns") == 9
        assert preferences.profiles == ["work"]
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_omitted_pack_arguments_take_the_person_s_sibling_defaults(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    preferences = FakeSettingsClient(
        {
            "search": {"default_result_count": 99, "search_backend": "searxng"},
            "spotify": {"default_device": "kitchen"},
        }
    )
    await container.preferences.aclose()
    container.preferences = preferences
    try:
        defaults = await container._pack_defaults("a.verified.jwt", "personal")
        assert defaults == {
            "research.limit": 20,
            "research.backend": "searxng",
            "music.device_id": "kitchen",
        }
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_nonsensical_sibling_defaults_are_omitted_rather_than_guessed(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    preferences = FakeSettingsClient(
        {
            "search": {"default_result_count": True, "search_backend": ""},
            "spotify": {"default_device": 3},
        }
    )
    await container.preferences.aclose()
    container.preferences = preferences
    try:
        assert await container._pack_defaults("a.verified.jwt", "personal") == {}
    finally:
        await container.aclose()


class SiblingOutage(FakeSettingsClient):
    """A settings seam that can confirm Lucy knobs but not a sibling namespace."""

    def __init__(self, error: SettingsClientError) -> None:
        super().__init__({"lucy": {"max_llm_turns": 12}})
        self.error = error

    async def resolve(
        self, namespace: str, *, user_token: str, profile: str | None = None
    ) -> ResolvedSettings:
        if namespace != "lucy":
            raise self.error
        return await super().resolve(namespace, user_token=user_token, profile=profile)


@pytest.mark.asyncio
async def test_sibling_namespaces_supply_pack_defaults(keyring: FakeKeyring) -> None:
    """What a pack falls back to when the model omits an argument comes from settings."""
    container = build_container(build_settings(), transport=keyring.transport())
    await container.preferences.aclose()
    container.preferences = FakeSettingsClient(
        {
            "search": {"default_result_count": 2, "search_backend": "google"},
            "spotify": {"default_device": "bedroom"},
        }
    )
    try:
        defaults = await container._pack_defaults("a.verified.jwt", "personal")
        assert defaults == {
            "research.limit": 2,
            "research.backend": "google",
            "music.device_id": "bedroom",
        }
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_a_sibling_settings_outage_does_not_take_the_turn_down(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    await container.preferences.aclose()
    container.preferences = SiblingOutage(SettingsUnavailable("down"))
    try:
        assert await container._pack_defaults("a.verified.jwt", "personal") == {}
    finally:
        await container.aclose()

    container = build_container(build_settings(), transport=keyring.transport())
    await container.preferences.aclose()
    container.preferences = SiblingOutage(SettingsRejected(403, "forbidden"))
    try:
        assert await container._pack_defaults("a.verified.jwt", "work") == {}
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_a_settings_outage_refuses_the_turn_rather_than_guessing_safety_limits(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    await container.preferences.aclose()
    container.preferences = FailingSettings(SettingsUnavailable("down"))
    try:
        assert await container._turn_settings("a.verified.jwt") is None
        with pytest.raises(LucyError) as refused:
            await container.prepare_turn(
                PackRequest(
                    caller=VerifiedCaller(account_id="acct_a", audience=AUDIENCE),
                    user_token="a.verified.jwt",
                    profile="personal",
                    session_id="ses_a",
                ),
                {"profile": "personal"},
            )
        assert refused.value.status == 503
        assert refused.value.code == "settings-unavailable"
        assert str(refused.value) == SETTINGS_UNAVAILABLE
    finally:
        await container.aclose()


@pytest.mark.asyncio
async def test_a_settings_authorization_failure_is_not_hidden_as_defaults(
    keyring: FakeKeyring,
) -> None:
    container = build_container(build_settings(), transport=keyring.transport())
    await container.preferences.aclose()
    container.preferences = FailingSettings(SettingsRejected(403, "grant missing"))
    try:
        with pytest.raises(SettingsRejected, match="grant missing"):
            await container._turn_settings("a.verified.jwt")
    finally:
        await container.aclose()


def test_an_unhandled_exception_does_not_echo_its_message() -> None:
    from lucy_api.api.errors import problem_response, unhandled_problem_response

    response = unhandled_problem_response(ValueError("secret path C:/hidden"))
    assert response.status_code == 500
    body = response.body.decode()
    assert "secret" not in body
    assert "hidden" not in body
    generic = problem_response(status_code=418, detail="no")
    assert generic.status_code == 418
    assert "error" in generic.body.decode()


@pytest.mark.asyncio
async def test_an_exploding_route_still_returns_a_request_id() -> None:
    from lucy_api.api.middleware import REQUEST_ID_HEADER, RequestContextMiddleware

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret-path")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/boom", headers={REQUEST_ID_HEADER: "req-from-caller"})
    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "req-from-caller"
    assert "secret-path" not in response.text


def test_a_misspelt_model_key_is_a_startup_error_that_opens_nothing(keyring: FakeKeyring) -> None:
    """The registry is validated before the database is opened.

    A typo in `LUCY_MODEL_KEYS` used to be found *after* the SQLite worker thread had
    started, which left that thread alive in a process that was refusing to start. The
    order is the fix, and this is the test that keeps it.
    """
    import threading

    from lucy_api.model.registry import UnknownModelError

    threads_before = threading.active_count()
    with pytest.raises(UnknownModelError, match="no catalogue row"):
        build_container(
            build_settings(model_keys={"gemeni": "sk-x"}), transport=keyring.transport()
        )
    assert threading.active_count() == threads_before, "no worker thread was started"
