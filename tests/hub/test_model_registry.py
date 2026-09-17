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
from lucy_api.model.openai import OpenAIProvider
from lucy_api.model.registry import (
    KNOWN,
    ModelRegistry,
    ModelSpec,
    UnknownModelError,
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


def test_a_provider_this_hub_has_never_heard_of_names_the_ones_it_has() -> None:
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    with pytest.raises(UnknownModelError) as raised:
        registry.resolve("gemini:pro")
    message = str(raised.value)
    assert "unknown model provider 'gemini'" in message
    assert "anthropic, openai" in message
    assert "configured for: openai" in message


def test_a_provider_with_no_credential_is_told_apart_from_one_that_does_not_exist() -> None:
    registry = http_registry({"openai": "sk-test"}, transport=NOWHERE)
    with pytest.raises(UnknownModelError) as raised:
        registry.resolve("anthropic:claude-opus-5")
    message = str(raised.value)
    assert "no credential is configured" in message
    assert "unknown" not in message, "this deployment is incomplete, not misspelt"


def test_a_registry_with_nothing_configured_says_so_rather_than_listing_an_empty_set() -> None:
    registry = http_registry({}, transport=NOWHERE)
    assert registry.providers == ()
    with pytest.raises(UnknownModelError, match="name one of: none"):
        registry.resolve("openai:gpt-5")


def test_a_credential_for_a_provider_we_have_no_adapter_for_is_a_typo_worth_refusing() -> None:
    with pytest.raises(UnknownModelError, match="no adapter for model provider\\(s\\): gemini"):
        http_registry({"gemini": "key", "openai": "sk-test"})


def test_an_empty_credential_configures_nothing_which_is_how_ready_reports_it() -> None:
    registry = http_registry({"anthropic": "", "openai": "sk-test"}, transport=NOWHERE)
    assert registry.providers == ("openai",)


async def test_each_known_provider_resolves_to_its_own_adapter() -> None:
    registry = http_registry({"anthropic": "sk-ant", "openai": "sk-test"}, transport=NOWHERE)
    assert registry.providers == KNOWN
    assert isinstance(registry.resolve("anthropic:claude-opus-5"), AnthropicProvider)
    assert isinstance(registry.resolve("openai:gpt-5"), OpenAIProvider)
    await registry.aclose()


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
