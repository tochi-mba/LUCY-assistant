"""Application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from hello_api import __version__
from hello_api.api.errors import register_exception_handlers
from hello_api.api.routers import ROUTERS
from hello_api.core.config import Settings, load_settings
from hello_api.core.container import build_container

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def create_app(
    settings: Settings | None = None,
    *,
    transport: object | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        settings: configuration; loaded from the environment when omitted.
        transport: optional httpx transport for tests (fake keyring).
    """
    resolved = settings if settings is not None else load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(resolved, transport=transport)
        app.state.container = container
        try:
            yield
        finally:
            await container.aclose()

    app = FastAPI(
        title="Hello API",
        description="Minimal LUCY-family example: health probes and one authenticated route.",
        version=__version__,
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    return app
