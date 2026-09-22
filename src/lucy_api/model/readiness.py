"""Which models a person can use right now, which they could, and what each needs.

A catalogue says what exists. A registry says what is configured. Neither says whether a
key is *live* -- whether the provider answers to it -- and that is the only question a
person choosing a model actually has. So every provider lands in exactly one of three
sections, and the section is a fact that was checked rather than a hope:

* **ready** -- configured, and a listing call answered. Usable this turn.
* **available** -- configured, but not proven: the provider documents no listing endpoint,
  or the check has not run yet. Usable, probably.
* **unavailable** -- not configured, or catalogued but unusable. Each carries the sentence
  that fixes it and the command to run, because "not available" on its own is where a
  person gives up.

## The check is cheap, cached, and never blocks a turn

Proving a key means one `GET` to the provider's models endpoint. That is a network call to
somebody else's service, so it has a short timeout, its answer is kept for a while, and it
is only ever made when somebody asks -- the models route, or the CLI. A turn never waits on
it: a turn resolves the provider it was told to use and finds out the ordinary way.

A local runtime is probed at its own health endpoint instead, unauthenticated, because
"is it running" is the whole question for something on this machine.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from lucy_api.model.catalogue import CATALOGUE, Auth, Binding, ProviderSpec
from lucy_api.model.registry import SETUP_COMMAND, bindings_for

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

PROBE_SECONDS = 4.0
"""How long a provider has to answer a listing call before it counts as not answering."""

CACHE_SECONDS = 60.0
"""How long a proven answer is trusted before it is asked again.

A minute is long enough that a page listing models does not hammer forty providers on
every refresh, and short enough that a key that was just added shows up before the person
has decided the feature is broken.
"""

KEY_SETTING = "LUCY_MODEL_KEYS"
URL_SETTING = "LUCY_MODEL_BASE_URLS"


@dataclass(frozen=True, slots=True)
class Setup:
    """What a person has to do to make a provider usable. Every field is a plain sentence."""

    command: str
    console_url: str = ""
    setting: str = KEY_SETTING
    instructions: str = ""


@dataclass(frozen=True, slots=True)
class Standing:
    """One provider, in one section, with the evidence for putting it there."""

    provider: str
    title: str
    section: str
    dialect: str
    models: tuple[str, ...]
    detail: str = ""
    setup: Setup | None = None
    local: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class Report:
    """Every catalogued provider, sorted into the three sections."""

    ready: tuple[Standing, ...] = ()
    available: tuple[Standing, ...] = ()
    unavailable: tuple[Standing, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": [_standing_dict(row) for row in self.ready],
            "available": [_standing_dict(row) for row in self.available],
            "unavailable": [_standing_dict(row) for row in self.unavailable],
        }

    def standing(self, provider: str) -> dict[str, Any]:
        """One provider's row, whichever section it landed in. `KeyError` for a name not here."""
        rows: dict[str, dict[str, Any]] = {
            row["provider"]: row for section in self.as_dict().values() for row in section
        }
        return rows[provider]


def _standing_dict(row: Standing) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": row.provider,
        "title": row.title,
        "section": row.section,
        "dialect": row.dialect,
        "models": list(row.models),
        "detail": row.detail,
        "local": row.local,
        "note": row.note,
    }
    if row.setup is not None:
        body["setup"] = {
            "command": row.setup.command,
            "console_url": row.setup.console_url,
            "setting": row.setup.setting,
            "instructions": row.setup.instructions,
        }
    return body


@dataclass(slots=True)
class _Proof:
    ok: bool
    detail: str
    checked_at: float


@dataclass(slots=True)
class Readiness:
    """Sorts the catalogue into sections, proving keys on demand and remembering answers."""

    api_keys: Mapping[str, str]
    base_urls: Mapping[str, str] = field(default_factory=dict)
    transport: httpx.AsyncBaseTransport | None = None
    clock: Callable[[], float] = time.monotonic
    probe_seconds: float = PROBE_SECONDS
    cache_seconds: float = CACHE_SECONDS
    _proofs: dict[str, _Proof] = field(default_factory=dict)

    async def report(self, *, prove: bool = True) -> Report:
        """Every provider in its section. `prove=False` skips the network entirely."""
        bindings = bindings_for(self.api_keys, self.base_urls)
        if prove:
            await asyncio.gather(*(self._prove(binding) for binding in bindings.values()))
        ready: list[Standing] = []
        available: list[Standing] = []
        unavailable: list[Standing] = []
        for spec in CATALOGUE:
            binding = bindings.get(spec.id)
            if binding is None:
                unavailable.append(_not_configured(spec))
                continue
            proof = self._proofs.get(spec.id)
            if proof is not None and proof.ok:
                ready.append(_standing(spec, "ready", proof.detail))
            elif proof is not None:
                unavailable.append(_standing(spec, "unavailable", proof.detail, setup=_setup(spec)))
            else:
                available.append(_standing(spec, "available", _unproven(spec)))
        return Report(tuple(ready), tuple(available), tuple(unavailable))

    async def _prove(self, binding: Binding) -> None:
        spec = binding.spec
        url = _probe_url(binding)
        if url is None:
            return
        cached = self._proofs.get(spec.id)
        if cached is not None and self.clock() - cached.checked_at < self.cache_seconds:
            return
        self._proofs[spec.id] = _Proof(*(await self._fetch(binding, url)), self.clock())

    async def _fetch(self, binding: Binding, url: str) -> tuple[bool, str]:
        """One GET, described as a sentence. Never the body: that is the provider's."""
        async with httpx.AsyncClient(
            timeout=self.probe_seconds, transport=self.transport, headers=_headers(binding)
        ) as http:
            try:
                response = await http.get(url)
            except httpx.HTTPError as exc:
                return False, f"could not be reached ({type(exc).__name__})"
        if response.status_code == httpx.codes.UNAUTHORIZED:
            return False, "the key was refused"
        if response.status_code == httpx.codes.FORBIDDEN:
            return False, "the key is not allowed to list models"
        if response.status_code >= httpx.codes.BAD_REQUEST:
            return False, f"answered {response.status_code}"
        return True, "answered" if not binding.spec.local else "running"


def _probe_url(binding: Binding) -> str | None:
    spec = binding.spec
    if spec.local:
        return spec.probe_url if not binding.base_url else binding.url.rstrip("/") + "/models"
    if spec.models_path is None:
        return None
    return binding.url.rstrip("/") + spec.models_path


def _headers(binding: Binding) -> dict[str, str]:
    spec, key = binding.spec, binding.api_key
    if spec.auth is Auth.bearer:
        return {"authorization": f"Bearer {key}"}
    if spec.auth is Auth.x_api_key:
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if spec.auth is Auth.api_key_header:
        return {"api-key": key}
    return {}


def _standing(
    spec: ProviderSpec, section: str, detail: str, *, setup: Setup | None = None
) -> Standing:
    return Standing(
        provider=spec.id,
        title=spec.title,
        section=section,
        dialect=spec.dialect.value,
        models=spec.models,
        detail=detail,
        setup=setup,
        local=spec.local,
        note=spec.note,
    )


def _unproven(spec: ProviderSpec) -> str:
    if spec.models_path is None and not spec.local:
        return "configured; this provider offers no way to check a key without spending"
    return "configured; not checked yet"


def _not_configured(spec: ProviderSpec) -> Standing:
    if not spec.usable:
        return _standing(spec, "unavailable", "cannot be used by this hub yet", setup=_setup(spec))
    detail = "not switched on" if spec.local else "no key configured"
    return _standing(spec, "unavailable", detail, setup=_setup(spec))


def _setup(spec: ProviderSpec) -> Setup:
    command = SETUP_COMMAND.format(provider=spec.id)
    if not spec.usable:
        return Setup(command="", console_url=spec.console_url, setting="", instructions=spec.note)
    if spec.local:
        return Setup(
            command=command,
            setting=URL_SETTING,
            instructions=(
                f"Start {spec.title} on this machine, then switch it on. It listens at "
                f"{spec.base_url} by default; name a different address if it runs elsewhere."
            ),
        )
    if spec.needs_base_url:
        return Setup(
            command=command,
            console_url=spec.console_url,
            setting=URL_SETTING,
            instructions=f"{spec.note} Then supply the key.",
        )
    return Setup(
        command=command,
        console_url=spec.console_url,
        instructions=f"Create a key at {spec.console_url} and supply it.",
    )


__all__ = [
    "CACHE_SECONDS",
    "KEY_SETTING",
    "PROBE_SECONDS",
    "URL_SETTING",
    "Readiness",
    "Report",
    "Setup",
    "Standing",
]
