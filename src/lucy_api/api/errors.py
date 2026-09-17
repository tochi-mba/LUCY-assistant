"""Map domain failures onto HTTP.

One shape for every failure, so a model reading a tool result has one error format to
reason about rather than one per route. The body never echoes the offending value: a 4xx
body is logged by the caller and handed back to a model, and a value that was refused for
looking like a credential must not then be copied into a log line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

from lucy_api.auth.verifier import AuthenticationError, KeyringUnreachableError

if TYPE_CHECKING:
    from fastapi import FastAPI

RETRY_AFTER_SECONDS = "5"


def register_exception_handlers(app: FastAPI) -> None:
    """Install the handlers for the two ways a caller can fail to be believed."""

    @app.exception_handler(AuthenticationError)
    async def _auth(_request: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(KeyringUnreachableError)
    async def _keys(_request: Request, exc: KeyringUnreachableError) -> JSONResponse:
        # 503, never 401. The token is probably fine; keyring is not.
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": RETRY_AFTER_SECONDS},
        )
