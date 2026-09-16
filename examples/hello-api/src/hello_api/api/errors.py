"""Map domain failures to HTTP responses."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse

from hello_api.auth.verifier import AuthenticationError, KeyringUnreachableError

if TYPE_CHECKING:
    from fastapi import FastAPI


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers for auth failures."""

    @app.exception_handler(AuthenticationError)
    async def _auth(_request: Request, exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(KeyringUnreachableError)
    async def _keys(_request: Request, exc: KeyringUnreachableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
