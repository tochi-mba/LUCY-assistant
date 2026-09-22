"""Resolving `provider:model`, and refusing it in the two different ways it can be wrong.

The interesting assertions here are the error messages. A name this hub has never heard
of and a name it knows but has no credential for look the same from the outside and have
opposite fixes, and a registry that says "unknown provider" to the second one sends an
operator to correct the one thing that was already right.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lucy_api.model.anthropic import AnthropicProvider
from lucy_api.model.catalogue import CATALOGUE
from lucy_api.model.chat import ChatProvider
from lucy_api.model.openai import OpenAIProvider
from lucy_api.model.registry import (
    KNOWN,
    ModelRegistry,
    ModelSpec,
    UnknownModelError,
    bindings_for,
    http_registry,
    parse_spec,
)
from lucy_api.model.scripted import ScriptedProvider, speaks
from lucy_api.model.types import Message, Request, Role


def ask() -> Request:
    return Request(messages=[Message(role=Role.user, content="hello")])


def answering(payload: dict[str, Any]) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _: httpx.Response(200, json=payload))


NOWHERE = answering({})
"""A transport for the tests that only care about resolution, so no real pool is opened."""


def test_a_spec_is_a_provider_and_a_model_either_side_of_one_colon() -> None:
    assert parse_spec("anthropic:claude-opus-5") == ModelSpec("anthropic", "claude-opus-5")
    assert str(parse_spec(" openai : gpt-5 ")) == "openai:gpt-5"


@pytest.mark.parametrize("spec", ["gpt-5", ":gpt-5", "openai:", "", ":"])
def test_a_spec_that_is_not_provider_colon_model_is_refused_with_the_shape_it_needed(
    spec,
) -> None:
    with pytest.raises(UnknownModelError, match="Write 'provider:model'"):
        parse_spec(spec)


def test_a_provider_this_hub_has_never_heard_of_points_at_the_catalogue() -> None:
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    with pytest.raises(UnknownModelError) as raised:
        registry.resolve("gemeni:pro")
    message = str(raised.value)
    assert "unknown model provider 'gemeni'" in message
    assert f"knows {len(KNOWN)} providers" in message
    assert "GET /v1/models" in message
    assert "configured for: openai" in message


def test_a_provider_with_nothing_configured_is_told_apart_from_one_that_does_not_exist() -> None:
    """The sentence carries the command that fixes it, because that is the whole point."""
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    with pytest.raises(UnknownModelError) as raised:
        registry.resolve("deepseek:deepseek-v4-pro")
    message = str(raised.value)
    assert "can talk to DeepSeek ('deepseek'), but nothing is configured" in message
    assert "Run `lucy models connect deepseek`" in message
    assert "unknown" not in message, "this deployment is incomplete, not misspelt"


def test_a_provider_this_hub_cannot_use_yet_says_why_rather_than_pretending() -> None:
    """Vertex needs an OAuth token this hub does not mint; that is the sentence."""
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    with pytest.raises(UnknownModelError, match="cannot be used yet: Needs an OAuth"):
        registry.resolve("vertex:gemini-3.1-pro")


def test_a_registry_with_nothing_configured_says_so_rather_than_listing_an_empty_set() -> None:
    registry = http_registry({}, transport=NOWHERE)
    assert registry.providers == ()
    with pytest.raises(UnknownModelError, match="name one of: none"):
        registry.resolve("openai:gpt-5")


def test_a_credential_for_a_provider_with_no_row_is_a_typo_worth_refusing() -> None:
    with pytest.raises(UnknownModelError, match="no catalogue row for model provider") as raised:
        http_registry({"gemeni": "key", "openai": "sk-test"})
    assert "gemeni" in str(raised.value)


def test_a_base_url_for_a_provider_with_no_row_is_refused_the_same_way() -> None:
    with pytest.raises(UnknownModelError, match="no catalogue row"):
        http_registry({}, base_urls={"olama": "http://localhost:11434/v1"})


def test_an_empty_credential_configures_nothing_which_is_how_ready_reports_it() -> None:
    registry = http_registry({"anthropic": "", "openai": "sk-test"}, transport=NOWHERE)
    assert registry.providers == ("openai",)


async def test_each_dialect_resolves_to_its_own_adapter() -> None:
    """Three adapters cover the catalogue; the row's dialect picks which."""
    registry = http_registry(
        {"anthropic": "sk-ant", "openai": "sk-test", "deepseek": "sk-deep", "xai": "sk-x"},
        transport=NOWHERE,
    )
    assert registry.providers == ("anthropic", "deepseek", "openai", "xai")
    assert isinstance(registry.resolve("anthropic:claude-opus-5"), AnthropicProvider)
    assert isinstance(registry.resolve("openai:gpt-5"), OpenAIProvider)
    assert isinstance(registry.resolve("xai:grok-4.7"), OpenAIProvider)
    deepseek = registry.resolve("deepseek:deepseek-v4-pro")
    assert isinstance(deepseek, ChatProvider)
    assert deepseek.name == "deepseek"
    assert registry.configured("deepseek")
    assert not registry.configured("moonshot")
    await registry.aclose()


async def test_a_local_runtime_needs_no_key_and_is_switched_on_by_naming_it() -> None:
    """Ollama has no credential to give; listing it is the whole configuration."""
    by_key = http_registry({"ollama": "local"}, transport=NOWHERE)
    assert by_key.providers == ("ollama",)
    by_url = http_registry({}, base_urls={"ollama": "http://gpu-box:11434/v1"}, transport=NOWHERE)
    assert by_url.providers == ("ollama",)
    assert http_registry({}, transport=NOWHERE).providers == ()
    await by_key.aclose()
    await by_url.aclose()


def test_a_row_that_needs_an_endpoint_is_refused_without_one() -> None:
    """Azure has no public host: a key alone would be a request to nowhere."""
    with pytest.raises(UnknownModelError, match="needs LUCY_MODEL_BASE_URLS"):
        http_registry({"azure-openai": "key"})


async def test_a_deployment_base_url_reaches_the_adapter() -> None:
    registry = http_registry(
        {"azure-openai": "key"},
        base_urls={"azure-openai": "https://tenant.openai.azure.com/openai/v1"},
        transport=NOWHERE,
    )
    provider = registry.resolve("azure-openai:my-deployment")
    assert isinstance(provider, ChatProvider)
    assert str(provider._http.base_url).startswith("https://tenant.openai.azure.com")
    await registry.aclose()


def test_a_provider_that_cannot_be_used_is_never_registered_even_with_a_key() -> None:
    assert http_registry({"vertex": "token"}, transport=NOWHERE).providers == ()


def test_every_catalogue_row_binds_when_given_what_it_asks_for() -> None:
    """The catalogue and the registry must agree about every row, not just the famous ones."""
    keys = {spec.id: "k" for spec in CATALOGUE}
    urls = {spec.id: "http://example.invalid/v1" for spec in CATALOGUE if spec.needs_base_url}
    bound = bindings_for(keys, urls)
    assert set(bound) == {spec.id for spec in CATALOGUE if spec.usable}


async def test_the_same_spec_resolves_to_the_same_provider_so_one_pool_is_opened_not_two() -> None:
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    assert registry.resolve("openai:gpt-5") is registry.resolve("openai:gpt-5")
    assert registry.resolve("openai:gpt-5") is not registry.resolve("openai:gpt-5-mini")
    await registry.aclose()


async def test_the_resolved_provider_is_bound_to_the_model_named_after_the_colon() -> None:
    registry = http_registry({"anthropic": "sk-ant"}, transport=NOWHERE)
    provider = registry.resolve("anthropic:claude-haiku-4-5")
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-haiku-4-5"
    assert provider.name == "anthropic"
    await registry.aclose()


async def test_a_resolved_provider_talks_over_the_transport_the_registry_was_given() -> None:
    payload = {
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "Tuesday."}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    registry = http_registry({"anthropic": "sk-ant"}, transport=answering(payload), timeout=1.0)
    reply = await registry.resolve("anthropic:claude-opus-5").complete(ask())
    assert reply.text == "Tuesday."
    await registry.aclose()


async def test_closing_the_registry_closes_what_it_built_and_forgets_it() -> None:
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    provider = registry.resolve("openai:gpt-5")
    assert isinstance(provider, OpenAIProvider)
    await registry.aclose()
    assert registry.resolve("openai:gpt-5") is not provider, "a closed provider is not reused"
    await registry.aclose()


async def test_a_provider_with_no_connection_pool_is_left_alone_when_the_registry_closes() -> None:
    registry = ModelRegistry({"scripted": lambda model: ScriptedProvider([speaks(model)])})
    provider = registry.resolve("scripted:golden")
    assert (await provider.complete(ask())).text == "golden"
    await registry.aclose()
