"""HTTP routers. Health comes first, because it is the one that must always answer."""

from __future__ import annotations

from lucy_api.api.routers import health, me

ROUTERS = (health.router, me.router)

__all__ = ["ROUTERS"]
