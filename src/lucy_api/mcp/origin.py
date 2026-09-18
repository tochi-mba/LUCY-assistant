"""DNS-rebinding defence for Streamable HTTP.

The Origin header is optional so CLI clients can call ``POST /mcp``. When it is
present it must be one of the hub's own origins, compared without echoing the
value that was refused: a 403 body is logged by whoever sent it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from lucy_api.core.errors import LucyError
from lucy_api.mcp.protocol import ORIGIN_REFUSED

if TYPE_CHECKING:
    from lucy_api.core.config import Settings

ORIGIN_CODE = "origin-refused"


def origin_refused() -> LucyError:
    return LucyError(ORIGIN_CODE, ORIGIN_REFUSED, 403)


def allowed_origins(settings: Settings) -> frozenset[str]:
    """The origins this process will answer as itself."""
    hosts = {settings.host.lower()}
    if settings.host in {"127.0.0.1", "localhost"}:
        hosts.update({"127.0.0.1", "localhost"})
    origins: set[str] = set()
    for host in hosts:
        origins.add(f"http://{host}:{settings.port}")
        origins.add(f"https://{host}:{settings.port}")
    return frozenset(origins)


def assert_origin(origin: str | None, settings: Settings) -> None:
    """Refuse a present Origin that is not this process. Missing Origin is allowed."""
    if origin is None or not origin.strip():
        return
    if _normalise(origin) not in allowed_origins(settings):
        raise origin_refused()


def _normalise(origin: str) -> str:
    parts = urlsplit(origin.strip().rstrip("/"))
    host = (parts.hostname or "").lower()
    scheme = parts.scheme.lower()
    if not scheme or not host:
        return ""
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}:{port}"
