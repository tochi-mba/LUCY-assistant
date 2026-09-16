"""HTTP routers."""

from __future__ import annotations

from hello_api.api.routers import health, whoami

ROUTERS = (health.router, whoami.router)

__all__ = ["ROUTERS"]
