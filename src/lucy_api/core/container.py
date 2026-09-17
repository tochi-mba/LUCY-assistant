"""The process-scoped object graph.

Everything with a lifetime longer than a request is built here once, in a FastAPI lifespan,
and hung on ``app.state.container``. Constructing the container does no network I/O, which
is what lets the whole hub be exercised in-process against a fake keyring by passing a
``transport``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from keyring_client import JwksClient, SystemClock

from lucy_api.auth.verifier import TokenVerifier

if TYPE_CHECKING:
    from lucy_api.core.config import Settings


@dataclass(slots=True)
class Container:
    """What every request shares for the life of the process."""

    settings: Settings
    jwks: JwksClient
    verifier: TokenVerifier
    started_at: float = field(default_factory=time.monotonic)

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    async def aclose(self) -> None:
        await self.jwks.aclose()


def build_container(settings: Settings, *, transport: object | None = None) -> Container:
    """Assemble the graph. No network I/O happens here, only on first use."""
    clock = SystemClock()
    jwks = JwksClient(
        url=settings.keyring_jwks_url,
        clock=clock,
        cache_seconds=settings.jwks_cache_seconds,
        min_refetch_seconds=settings.jwks_min_refetch_seconds,
        timeout_seconds=settings.http_timeout_seconds,
        transport=transport,  # type: ignore[arg-type]
    )
    verifier = TokenVerifier(
        jwks=jwks,
        issuer=settings.keyring_issuer,
        audience=settings.audience,
        clock=clock,
    )
    return Container(settings=settings, jwks=jwks, verifier=verifier)
