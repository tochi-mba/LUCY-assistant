"""A conversation starts on a model this hub can run, or is refused with the reason.

On a family whose only provider was clyde, `lucy talk` created every conversation on the
catalogue's default -- `anthropic:claude-opus-5`, with no Anthropic key anywhere -- and the
first turn failed in 22 milliseconds with `model configuration failed (UnknownModelError)`,
`error_code: null`, and nothing a person could act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import bearer, build_settings
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.core.errors import LucyError
from lucy_api.model.readiness import Report, Standing
from lucy_api.settings.catalogue import DEFAULT_MODEL

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from conftest import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container

CLYDE = {"clyde": "http://clyde.test/v1"}


@dataclass
class Hub:
    http: AsyncClient
    container: Container


@pytest.fixture
async def hub(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[Hub]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.environment_override = FakeEnvironmentsClient()
        yield Hub(http=http, container=container)


def _row(provider: str, *models: str, section: str = "ready") -> Standing:
    return Standing(
        provider=provider, title=provider, section=section, dialect="openai-chat", models=models
    )


class _Readiness:
    """A readiness that answers with a fixed report, and says whether it was asked."""

    def __init__(self, report: Report | None) -> None:
        self._report = report
        self.asked = False

    async def report(self, *, prove: bool = True) -> Report:
        del prove
        self.asked = True
        if self._report is None:
            raise AssertionError("readiness was consulted")
        return self._report


def _report_as(container: Container, report: Report | None) -> _Readiness:
    fixed = _Readiness(report)
    container.readiness = fixed  # type: ignore[assignment]
    return fixed


async def _create(hub: Hub, **body: Any) -> Any:
    return await hub.http.post(
        "/v1/sessions", json=body, headers={**bearer(), "Idempotency-Key": "k"}
    )


@pytest.mark.parametrize("settings", [build_settings(model_base_urls=CLYDE)])
async def test_an_unrunnable_default_falls_back_to_a_model_the_hub_can_run(hub: Hub) -> None:
    """The bug, named: nobody chose a model, the default cannot run here, clyde can."""
    _report_as(hub.container, Report((_row("clyde", "sonnet", "haiku"),), (), ()))
    response = await _create(hub)
    assert response.status_code == 201, response.text
    assert response.json()["model"] == "clyde:sonnet"


@pytest.mark.parametrize("settings", [build_settings(model_base_urls=CLYDE)])
async def test_a_configured_but_unchecked_provider_is_the_next_choice(hub: Hub) -> None:
    _report_as(hub.container, Report((), (_row("clyde", "opus", section="available"),), ()))
    response = await _create(hub)
    assert response.json()["model"] == "clyde:opus"


@pytest.mark.parametrize("settings", [build_settings(model_base_urls=CLYDE)])
async def test_a_provider_with_no_known_model_or_that_cannot_be_built_is_passed_over(
    hub: Hub,
) -> None:
    """A listing can name a provider this registry never built, and a local runtime can be
    up without saying what it runs. Neither is a model a conversation can start on."""
    _report_as(
        hub.container,
        Report((_row("ghost", "phantom"), _row("clyde")), (_row("clyde", "haiku"),), ()),
    )
    response = await _create(hub)
    assert response.json()["model"] == "clyde:haiku"


async def test_with_nothing_configured_the_default_stands_and_the_turn_will_say_why(
    hub: Hub,
) -> None:
    """There is no better choice to make, and refusing to create the conversation would help
    nobody: its first turn now fails with the registry's instructions instead."""
    _report_as(hub.container, Report((), (), ()))
    response = await _create(hub)
    assert response.status_code == 201
    assert response.json()["model"] == DEFAULT_MODEL


async def test_an_explicit_model_the_hub_cannot_run_is_refused_with_the_reason(hub: Hub) -> None:
    response = await _create(hub, model="openai:gpt-5")
    assert response.status_code == 422
    problem = response.json()
    assert problem["type"].endswith("model-unavailable")
    assert "lucy models connect openai" in problem["detail"]


@pytest.mark.parametrize("settings", [build_settings(model_base_urls=CLYDE)])
async def test_an_explicit_model_the_hub_can_run_is_kept(hub: Hub) -> None:
    response = await _create(hub, model="clyde:haiku")
    assert response.status_code == 201
    assert response.json()["model"] == "clyde:haiku"


async def test_a_default_somebody_chose_is_never_swapped_silently(hub: Hub) -> None:
    """Only the catalogue's own default is replaced. A person's choice that cannot run here is
    refused with the reason, because switching it without asking would be worse than no."""
    with pytest.raises(LucyError) as raised:
        await hub.container._runnable_default("openai:gpt-5")
    assert raised.value.status == 422
    assert "openai" in str(raised.value)


@pytest.mark.parametrize("settings", [build_settings(model_base_urls=CLYDE)])
async def test_a_chosen_default_the_hub_can_run_is_kept_without_checking_anything(
    hub: Hub,
) -> None:
    fixed = _report_as(hub.container, None)
    assert await hub.container._runnable_default("clyde:opus") == "clyde:opus"
    assert fixed.asked is False
