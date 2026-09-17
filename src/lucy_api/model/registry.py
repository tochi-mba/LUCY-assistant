"""One string names one model, and the string is what everything else stores.

A session records `model` as `anthropic:claude-opus-5`, a setting names a default the
same way, and a request may override it. Keeping that one spelling means the provider is
a lookup rather than a branch, and it means the thing written into the database is the
thing a person typed -- so a session that was answered by one model is never silently
answered by another after a config change.

## An unknown provider is a wrong question

"Errors name the fix" applies with force here, because the two ways to get this wrong
look identical from the outside and have opposite remedies. A name this hub has never
heard of is a typo in a settings value. A name it knows perfectly well but has no
credential for is an unconfigured deployment, and telling that person "unknown provider"
would send them to fix the one thing that is not broken. The two get different sentences.

## Providers are resolved once and kept

Each real provider owns an HTTP connection pool, so resolving the same spec twice must
not open a second one. The registry caches by spec and closes what it made, which makes
it the thing an application's lifespan holds rather than a free function.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from lucy_api.model.anthropic import AnthropicProvider
from lucy_api.model.openai import OpenAIProvider
from lucy_api.model.wire import DEFAULT_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx

    from lucy_api.model.types import Provider

KNOWN = ("anthropic", "openai")
"""Every provider this hub has an adapter for, whether or not one is configured."""

type ProviderFactory = Callable[[str], Provider]
"""Builds a provider bound to one model id -- the half after the colon."""


class UnknownModelError(Exception):
    """The spec did not name a model this hub can reach, and says which ones it can."""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A parsed `provider:model`."""

    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def parse_spec(spec: str) -> ModelSpec:
    """Split `provider:model`, or refuse with the shape it should have had."""
    provider, separator, model = spec.partition(":")
    provider, model = provider.strip(), model.strip()
    if not separator or not provider or not model:
        msg = (
            f"{spec!r} is not a model spec. Write 'provider:model' -- for example "
            f"'anthropic:claude-opus-5'. Known providers: {', '.join(KNOWN)}."
        )
        raise UnknownModelError(msg)
    return ModelSpec(provider=provider, model=model)


class ModelRegistry:
    """Turns a spec into something that can think, and owns what it built."""

    def __init__(self, factories: Mapping[str, ProviderFactory]) -> None:
        self._factories = dict(factories)
        self._resolved: dict[str, Provider] = {}

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers this registry can actually build, in alphabetical order."""
        return tuple(sorted(self._factories))

    def resolve(self, spec: str) -> Provider:
        """The provider for `spec`, built once and kept."""
        parsed = parse_spec(spec)
        factory = self._factories.get(parsed.provider)
        if factory is None:
            raise UnknownModelError(self._unknown(parsed.provider))
        key = str(parsed)
        if key not in self._resolved:
            self._resolved[key] = factory(parsed.model)
        return self._resolved[key]

    async def aclose(self) -> None:
        """Close every provider that has a connection pool to give back."""
        for provider in self._resolved.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()
        self._resolved.clear()

    def _unknown(self, provider: str) -> str:
        configured = ", ".join(self.providers) or "none"
        if provider in KNOWN:
            return (
                f"this hub can talk to {provider!r}, but no credential is configured for "
                f"it. Connect a model provider, or name one of: {configured}."
            )
        return (
            f"unknown model provider {provider!r}. This hub has adapters for "
            f"{', '.join(KNOWN)}, and is configured for: {configured}."
        )


def _anthropic(
    api_key: str, transport: httpx.AsyncBaseTransport | None, timeout: float, model: str
) -> Provider:
    return AnthropicProvider(api_key=api_key, model=model, timeout=timeout, transport=transport)


def _openai(
    api_key: str, transport: httpx.AsyncBaseTransport | None, timeout: float, model: str
) -> Provider:
    return OpenAIProvider(api_key=api_key, model=model, timeout=timeout, transport=transport)


def http_registry(
    api_keys: Mapping[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ModelRegistry:
    """A registry over the providers there is a credential for.

    A key for a provider this hub has no adapter for is refused rather than ignored: it
    is a typo in configuration, and the deployment that silently drops it fails later, at
    the first turn, in a place that does not mention the setting. An empty key registers
    nothing, which is how `/ready` comes to say the model is not connected yet.
    """
    unknown = sorted(set(api_keys) - set(KNOWN))
    if unknown:
        msg = (
            f"no adapter for model provider(s): {', '.join(unknown)}. This hub has "
            f"adapters for {', '.join(KNOWN)}."
        )
        raise UnknownModelError(msg)
    factories: dict[str, ProviderFactory] = {}
    for provider, api_key in api_keys.items():
        if not api_key:
            continue
        builder = _anthropic if provider == "anthropic" else _openai
        factories[provider] = partial(builder, api_key, transport, timeout)
    return ModelRegistry(factories)


__all__ = [
    "KNOWN",
    "ModelRegistry",
    "ModelSpec",
    "ProviderFactory",
    "UnknownModelError",
    "http_registry",
    "parse_spec",
]
