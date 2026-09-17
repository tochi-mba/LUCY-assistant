"""Setup must distinguish deployment health from a person's connected accounts."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from asgi_lifespan import LifespanManager
from keyring_client.testing import ISSUER, mint

from lucy_api.api.app import create_app
from lucy_api.core.config import ExtraSibling
from lucy_api.onboarding.catalogue import extra_manifests, manifests
from lucy_api.onboarding.models import SetupCheck
from lucy_api.onboarding.service import HttpSetupProbe, ProbeResult, SetupDiscovery

ACCOUNT = "acct_example"


def bearer(account_id=ACCOUNT):
    token = mint(account_id=account_id, audience="lucy-api", issuer=ISSUER)
    return {"Authorization": f"Bearer {token}"}


class FakeProbe:
    """A hand-written seam fake records discovery without needing a running family."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def read(self, manifest):
        self.seen.append(manifest.id)
        states = {"music": "ready", "identity": "degraded"}
        return ProbeResult(states.get(manifest.id, "unavailable"))


async def test_discovery_keeps_optional_music_and_account_state_independent(settings):
    probe = FakeProbe()
    discovery = SetupDiscovery(settings, probe)
    result = await discovery.discover(ACCOUNT)
    assert result.account_id == ACCOUNT
    assert [row.id for row in result.services] == probe.seen
    assert len(result.services) == 8
    services = {row.id: row for row in result.services}
    assert [row.id for row in result.services if row.required] == ["identity"]
    assert services["music"].state == "ready"
    assert services["music"].connection_state == "unknown"
    assert not services["music"].required
    assert "does not verify" in services["music"].summary
    assert "dependency problem" in services["identity"].summary
    assert "could not be verified" in services["workspace"].summary
    assert services["workspace"].connection_state == "not_required"
    instructions = services["music"].actions[0].description
    assert "your own account" in instructions
    assert "stores and refreshes" in instructions
    assert "cannot yet start" in instructions
    for row in result.services:
        assert [action.kind for action in row.actions] == ["operator", "documentation"]
        assert row.actions[0].url is None
        assert row.actions[1].url.startswith("https://github.com/tochi-mba/")


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, {}, "ready"),
        (503, {}, "degraded"),
        (404, {}, "unavailable"),
        (302, {}, "unavailable"),
        (200, [], "unavailable"),
        (200, "invalid", "unavailable"),
    ],
)
async def test_probe_http_outcomes_are_safe(settings, status, payload, expected):
    requests = []

    def respond(request):
        requests.append(request)
        if payload == "invalid":
            return httpx.Response(status, content=b"not json")
        return httpx.Response(status, json=payload, headers={"Location": "https://example.invalid"})

    probe = HttpSetupProbe(settings, transport=httpx.MockTransport(respond))
    try:
        result = await probe.read(replace(manifests(settings)[0], base_url="http://identity/"))
    finally:
        await probe.aclose()
    assert result == ProbeResult(expected)
    assert len(requests) == 1
    assert str(requests[0].url) == "http://identity/ready"
    assert "authorization" not in requests[0].headers
    assert "x-keyring-user-token" not in requests[0].headers


async def test_probe_timeout_never_returns_exception_contents(settings):
    def fail(request):
        raise httpx.ReadTimeout("a-secret-in-the-network-exception", request=request)

    probe = HttpSetupProbe(settings, transport=httpx.MockTransport(fail))
    try:
        assert await probe.read(manifests(settings)[0]) == ProbeResult("unavailable")
    finally:
        await probe.aclose()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "checks": {
                    "database": {"status": "ok", "detail": "secret"},
                    "keyring": {"status": "degraded"},
                    "secret": {"status": "ok"},
                }
            },
            [("database", "ready"), ("keyring", "degraded")],
        ),
        (
            {
                "checks": [
                    {"name": "database", "ready": True},
                    {"name": "keyring", "ready": False},
                    None,
                    {"name": 42, "ready": True},
                    {"ready": True},
                ]
            },
            [("database", "ready"), ("keyring", "degraded")],
        ),
        (
            {
                "components": [
                    {"name": "database", "status": "ready"},
                    {"name": "keyring", "status": "not_ready"},
                ]
            },
            [("database", "ready"), ("keyring", "degraded")],
        ),
        (
            {"checks": {"database": None, "keyring": {"status": {"not": "a string"}}}},
            [("database", "unknown"), ("keyring", "unknown")],
        ),
        ({"checks": "invalid"}, []),
        ({}, []),
    ],
)
async def test_probe_projects_only_known_dependency_names(settings, payload, expected):
    probe = HttpSetupProbe(
        settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    try:
        report = await probe.read(manifests(settings)[1])
    finally:
        await probe.aclose()
    assert report.checks == tuple(SetupCheck(name=name, state=state) for name, state in expected)
    assert "secret" not in repr(report)


async def test_route_authenticates_and_never_sends_caller_token_to_readiness(keyring, settings):
    upstream_requests = []
    keys = keyring.transport()

    async def respond(request):
        if request.url.path == "/.well-known/jwks.json":
            return await keys.handle_async_request(request)
        upstream_requests.append(request)
        if request.url.port == 8007:
            return httpx.Response(200, json={"checks": {"spotify": {"status": "ok"}}})
        return httpx.Response(503, json={"detail": "never-echo-this-secret"})

    app = create_app(settings, transport=httpx.MockTransport(respond))
    async with (
        LifespanManager(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        missing = await client.get("/v1/setup")
        assert missing.status_code == 401
        assert upstream_requests == []
        response = await client.get("/v1/setup", headers=bearer())
        assert response.status_code == 200
        assert response.json()["account_id"] == ACCOUNT
        music = next(row for row in response.json()["services"] if row["id"] == "music")
        assert music["checks"] == [{"name": "spotify", "state": "ready"}]
        assert music["state"] == "ready"
        assert music["connection_state"] == "unknown"
        assert "never-echo-this-secret" not in response.text
        assert len(upstream_requests) == 8
        assert all(request.url.path == "/ready" for request in upstream_requests)
        assert all("authorization" not in request.headers for request in upstream_requests)
        assert all("x-keyring-user-token" not in request.headers for request in upstream_requests)


async def test_setup_uses_verified_subject_without_caching_another_account(client):
    one = await client.get("/v1/setup", headers=bearer("acct_one"))
    two = await client.get("/v1/setup", headers=bearer("acct_two"))
    assert one.json()["account_id"] == "acct_one"
    assert two.json()["account_id"] == "acct_two"


def test_setup_schema_explains_unknown_connection_state(settings):
    schema = create_app(settings).openapi()
    operation = schema["paths"]["/v1/setup"]["get"]
    assert operation["operationId"] == "get_setup"
    assert "cannot yet inspect" in operation["description"]
    assert operation["security"]


# --------------------------------------------------------------------------------------
# Capabilities only this machine has. See docs/adr/0011-private-services-are-extensions.md.
# --------------------------------------------------------------------------------------


def an_extra(**overrides):
    fields = {"base_url": "http://127.0.0.1:9100"}
    return ExtraSibling(**{**fields, **overrides})


def test_a_capability_only_this_machine_has_gets_a_card_without_the_hub_knowing_it(settings):
    """The hub special-cases nothing.

    A branch here reading ``if capability == "..."`` would name a private service in a
    public file, and the second private service would need a second branch. What this
    build knows is that extension points exist -- never what might attach to one.
    """
    configured = settings.model_copy(update={"extra_services": {"archive": an_extra()}})

    (card,) = extra_manifests(configured)

    assert card.id == "archive"
    assert card.title == "Archive", "a readable title is derived rather than demanded"
    assert card.checks == ("ready",)
    assert card.connection_state == "unknown"
    assert card.documentation.endswith("docs/private-repos.md")
    assert "operator-local service" in card.instructions


def test_an_operator_who_wants_a_better_card_writes_one_rather_than_patching_the_hub(settings):
    configured = settings.model_copy(
        update={
            "extra_services": {
                "archive": an_extra(
                    title="Cold storage",
                    instructions="Point it at the vault and give it a credential.",
                    checks=("identity", "storage"),
                    documentation="https://example.invalid/archive",
                )
            }
        }
    )

    (card,) = extra_manifests(configured)

    assert card.title == "Cold storage"
    assert card.instructions == "Point it at the vault and give it a credential."
    assert card.checks == ("identity", "storage")
    assert card.documentation == "https://example.invalid/archive"


def test_an_extra_with_no_address_is_the_same_as_not_having_configured_one(settings):
    """A half-written entry in an operator's file must not become a card nobody can use."""
    configured = settings.model_copy(
        update={"extra_services": {"archive": an_extra(base_url="   ")}}
    )

    assert extra_manifests(configured) == ()


def test_extras_sit_between_the_built_ins_so_published_identifiers_stay_stable(settings):
    configured = settings.model_copy(update={"extra_services": {"archive": an_extra()}})

    ids = [manifest.id for manifest in manifests(configured)]

    assert ids.index("archive") == ids.index("notes") + 1
    assert ids[0] == "identity", "the required one is still first"
