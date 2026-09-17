"""Read the family's public readiness reports without disclosing caller credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Protocol

import httpx

from lucy_api.onboarding.catalogue import SetupManifest, manifests
from lucy_api.onboarding.models import (
    CheckState,
    Readiness,
    SetupCheck,
    SetupResponse,
    SetupService,
)

if TYPE_CHECKING:
    from lucy_api.core.config import Settings


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The sanitized projection of one readiness call."""

    state: Readiness
    checks: tuple[SetupCheck, ...] = ()


class SetupProbe(Protocol):
    """The seam between setup discovery and network I/O."""

    async def read(self, manifest: SetupManifest) -> ProbeResult:
        """Return sanitized deployment state; never resolve a provider credential."""


class HttpSetupProbe:
    """One timeout-bounded probe per configured service, with redirects disabled."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Release connections when the application stops."""
        await self._http.aclose()

    async def read(self, manifest: SetupManifest) -> ProbeResult:
        """Read public health only. Exceptions and raw payloads never enter the response."""
        try:
            response = await self._http.get(manifest.base_url.rstrip("/") + "/ready")
            if response.status_code not in {HTTPStatus.OK, HTTPStatus.SERVICE_UNAVAILABLE}:
                return ProbeResult("unavailable")
            payload: object = response.json()
        except (httpx.HTTPError, ValueError):
            return ProbeResult("unavailable")
        if not isinstance(payload, dict):
            return ProbeResult("unavailable")
        return ProbeResult(
            "ready" if response.status_code == HTTPStatus.OK else "degraded",
            _checks(payload, manifest.checks),
        )


def _checks(payload: dict[str, object], allowed: tuple[str, ...]) -> tuple[SetupCheck, ...]:
    """Adapt the family's dict/list health formats without forwarding free-form details."""
    source = payload.get("checks", payload.get("components"))
    found: dict[str, object] = {}
    if isinstance(source, dict):
        found = source
    elif isinstance(source, list):
        for item in source:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                found[item["name"]] = item
    return tuple(
        SetupCheck(name=name, state=_check_state(found[name])) for name in allowed if name in found
    )


def _check_state(value: object) -> CheckState:
    if isinstance(value, dict):
        if value.get("ready") is True or value.get("status") in ("ok", "ready"):
            return "ready"
        if value.get("ready") is False or value.get("status") in ("degraded", "not_ready"):
            return "degraded"
    return "unknown"


class SetupDiscovery:
    """Fan out independent probes; one unavailable optional service cannot hide the rest."""

    def __init__(self, settings: Settings, probe: SetupProbe) -> None:
        self._manifests = manifests(settings)
        self._probe = probe

    async def discover(self, account_id: str) -> SetupResponse:
        """Project deployment status beside explicit limits on per-account discovery."""
        reports = await asyncio.gather(*(self._probe.read(item) for item in self._manifests))
        summaries: dict[Readiness, str] = {
            "ready": "Deployment is ready. This does not verify your provider connections.",
            "degraded": "Deployment reports a dependency problem; review its setup steps.",
            "unavailable": "Readiness could not be verified; check deployment and configuration.",
        }
        return SetupResponse(
            account_id=account_id,
            services=[
                SetupService(
                    id=manifest.id,
                    title=manifest.title,
                    required=manifest.required,
                    state=report.state,
                    connection_state=manifest.connection_state,
                    summary=summaries[report.state],
                    checks=list(report.checks),
                    actions=manifest.actions(),
                )
                for manifest, report in zip(self._manifests, reports, strict=True)
            ],
        )
