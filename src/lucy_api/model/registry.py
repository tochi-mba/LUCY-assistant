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
would send them to fix the one thing that is not broken. The two get different sentences,
and the second one carries the command that fixes it.

## The catalogue decides the adapter

Every provider is a row in `lucy_api.model.catalogue`, and the row's dialect picks the
adapter: Messages, Responses, or the chat-completions format everybody else speaks. The
registry never names a provider itself. Adding one is adding a row.

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
from lucy_api.model.catalogue import CATALOGUE, KNOWN, Binding, Dialect, ProviderSpec, spec_for
from lucy_api.model.chat import ChatProvider
from lucy_api.model.openai import OpenAIProvider
from lucy_api.model.wire import DEFAULT_TIMEOUT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx

    from lucy_api.model.types import Provider

type ProviderFactory = Callable[[str], Provider]
"""Builds a provider bound to one model id -- the half after the colon."""

SETUP_COMMAND = "lucy models connect {provider}"
"""What a person runs to supply a credential. Named here so every sentence agrees."""


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
            f"'anthropic:claude-opus-5'. GET /v1/models lists every provider."
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

    def configured(self, provider: str) -> bool:
        return provider in self._factories

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
        spec = spec_for(provider)
        if spec is not None and spec.usable:
            fix = SETUP_COMMAND.format(provider=provider)
            return (
                f"this hub can talk to {spec.title} ({provider!r}), but nothing is configured "
                f"for it. Run `{fix}`, or name one of: {configured}."
            )
        if spec is not None:
            return (
                f"{spec.title} ({provider!r}) is catalogued but cannot be used yet: "
                f"{spec.note} Name one of: {configured}."
            )
        return (
            f"unknown model provider {provider!r}. This hub knows {len(KNOWN)} providers; "
            f"GET /v1/models lists them. It is configured for: {configured}."
        )


def _build(
    binding: Binding,
    transport: httpx.AsyncBaseTransport | None,
    timeout: float,
    model: str,
) -> Provider:
    """The adapter the row's dialect calls for, bound to one model."""
    if binding.spec.dialect is Dialect.anthropic_messages:
        return AnthropicProvider(
            api_key=binding.api_key,
            model=model,
            base_url=binding.url,
            timeout=timeout,
            transport=transport,
        )
    if binding.spec.dialect is Dialect.openai_responses:
        return OpenAIProvider(
            api_key=binding.api_key,
            model=model,
            base_url=binding.url,
            timeout=timeout,
            transport=transport,
        )
    return ChatProvider(binding, model, timeout=timeout, transport=transport)


def bindings_for(
    api_keys: Mapping[str, str], base_urls: Mapping[str, str] | None = None
) -> dict[str, Binding]:
    """What this deployment can build: one binding per provider it has enough for.

    A key for a provider this hub has no row for is refused rather than ignored: it is a
    typo in configuration, and the deployment that silently drops it fails later, at the
    first turn, in a place that does not mention the setting. A row that needs a base URL
    and was not given one is refused the same way, because the alternative is a request to
    an empty host.

    A local runtime needs no key; naming it with any value, or giving it a base URL, is
    what switches it on. An empty key registers nothing, which is how `/ready` comes to say
    the model is not connected yet.
    """
    urls = dict(base_urls or {})
    unknown = sorted((set(api_keys) | set(urls)) - set(KNOWN))
    if unknown:
        msg = (
            f"no catalogue row for model provider(s): {', '.join(unknown)}. "
            "GET /v1/models lists the ones this hub knows."
        )
        raise UnknownModelError(msg)
    bound: dict[str, Binding] = {}
    for spec in CATALOGUE:
        key = api_keys.get(spec.id, "")
        url = urls.get(spec.id, "")
        if not _enough(spec, key, url):
            continue
        if spec.needs_base_url and not url:
            msg = (
                f"{spec.title} ({spec.id!r}) needs LUCY_MODEL_BASE_URLS to name its "
                f"endpoint: {spec.note}"
            )
            raise UnknownModelError(msg)
        bound[spec.id] = Binding(spec, api_key=key, base_url=url)
    return bound


def _enough(spec: ProviderSpec, key: str, url: str) -> bool:
    """Whether this deployment supplied what the row needs to be built at all."""
    if not spec.usable:
        return False
    if spec.needs_key:
        return bool(key)
    return bool(key or url)


def http_registry(
    api_keys: Mapping[str, str],
    *,
    base_urls: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> ModelRegistry:
    """A registry over the providers there is enough configuration for."""
    factories: dict[str, ProviderFactory] = {
        provider: partial(_build, binding, transport, timeout)
        for provider, binding in bindings_for(api_keys, base_urls).items()
    }
    return ModelRegistry(factories)


__all__ = [
    "KNOWN",
    "SETUP_COMMAND",
    "ModelRegistry",
    "ModelSpec",
    "ProviderFactory",
    "UnknownModelError",
    "bindings_for",
    "http_registry",
    "parse_spec",
]
