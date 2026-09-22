"""HTTP routers, in the order Starlette will try them.

Health comes first, because it is the one that must always answer. The session router is
last because it is the only one with parameterised paths: FastAPI 0.141 ranks an included
router's literal path ahead of another router's parameterised one, so today this order is
belt as well as braces -- but that ranking is a detail of a dependency, it has changed
before, and the failure it would cause is silent. A `/v1/me` answered as a lookup for a
session called "me" is the kind of break that ships.
"""

from __future__ import annotations

from lucy_api.api.routers import (
    agents,
    capabilities,
    connections,
    device,
    economy,
    files,
    health,
    mcp,
    mcp_servers,
    me,
    models,
    permissions,
    prompt,
    sessions,
    webhooks,
    wellknown,
)

ROUTERS = (
    health.router,
    wellknown.router,
    mcp.router,
    mcp_servers.router,
    device.router,
    me.router,
    models.router,
    connections.router,
    capabilities.router,
    prompt.router,
    permissions.router,
    economy.router,
    files.router,
    webhooks.router,
    agents.router,
    sessions.router,
)

__all__ = ["ROUTERS"]
