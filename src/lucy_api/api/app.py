"""The application factory.

``transport`` exists so the whole hub can be driven in-process against a fake keyring. It
is the seam that lets this service reach full coverage without a live dependency, and it is
the same seam the family's other services use.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from lucy_api import __version__
from lucy_api.api.errors import register_exception_handlers
from lucy_api.api.middleware import RequestContextMiddleware
from lucy_api.api.routers import ROUTERS
from lucy_api.api.routers.setup import router as setup_router
from lucy_api.core.config import LogFormat, Settings, load_settings
from lucy_api.core.container import build_container
from lucy_api.core.logging import configure
from lucy_api.onboarding.service import HttpSetupProbe, SetupDiscovery

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from httpx import AsyncBaseTransport

TITLE = "Lucy"
DESCRIPTION = (
    "The assistant hub: one conversation, the capabilities this person has connected, "
    "a workspace, memory, and sub-agents."
)


def create_app(
    settings: Settings | None = None,
    *,
    transport: AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the application.

    Args:
        settings: configuration; loaded from the environment when omitted.
        transport: optional httpx transport for tests (a fake keyring).
    """
    resolved = settings if settings is not None else load_settings()
    if resolved.log_format is LogFormat.JSON:
        configure(level=resolved.log_level, file=resolved.log_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        container = build_container(resolved, transport=transport)
        app.state.container = container
        probe = HttpSetupProbe(resolved, transport=transport)
        app.state.onboarding = SetupDiscovery(resolved, probe)
        try:
            # Inside the try: a schema migration that fails still has to give the worker
            # thread back, or the process hangs on the way out of a failed startup.
            await container.start()
            yield
        finally:
            try:
                await probe.aclose()
            finally:
                await container.aclose()

    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    # Outermost, so the request id is bound before anything else runs and is still bound
    # when an unhandled exception is turned into the 500 that tells the caller to quote it.
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    app.include_router(setup_router)
    return app
