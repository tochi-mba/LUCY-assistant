"""Map domain failures onto HTTP.

One shape for every failure, so a model reading a tool result has one error format to
reason about rather than one per route. The body never echoes the offending value: a 4xx
body is logged by the caller and handed back to a model, and a value that was refused for
looking like a credential must not then be copied into a log line.

Three of the mappings are decisions rather than lookups.

**A session belonging to somebody else is 404, never 403.** A 403 confirms the id exists,
and an identifier is the one fact that must not cross between accounts. The store already
answers ``absent()`` for a foreign row; this is the half of that promise that faces the
network.

**Keyring being unreachable is 503 with ``Retry-After``, not 401.** They mean opposite
things to the person holding the token. Telling somebody to log in again because this
service could not fetch a public key is advice that does not help.

**An unexpected exception's text never reaches the caller.** It can carry a path, a
hostname, or a sentence somebody said to Lucy. The caller gets a request id to quote and
the log record on this side has the rest.

Domain failures are not listed in a table here. :class:`~lucy_api.core.errors.LucyError`
already carries its own ``status`` and ``code``, so one handler registered against the base
class serves every one of them -- Starlette walks the exception's MRO looking for a match. A
table would be a second place to edit whenever an error is added, and the failure mode of
forgetting is a 500 for something the hub understood perfectly well.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lucy_api.api.schemas.problem import PROBLEM_CONTENT_TYPE, FieldError, Problem
from lucy_api.auth.device import DeviceFlowError
from lucy_api.auth.verifier import AuthenticationError, KeyringUnreachableError
from lucy_api.core.errors import LucyError
from lucy_api.core.request_id import get_request_id
from lucy_api.mcp.dispatch import bearer_challenge

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

PROBLEM_BASE_URI = "https://lucy-api.invalid/problems"
RETRY_AFTER_HEADER = "Retry-After"
RETRY_AFTER_SECONDS = "5"

_STATUS_TITLES = {
    status.HTTP_400_BAD_REQUEST: "Bad request",
    status.HTTP_401_UNAUTHORIZED: "Unauthorized",
    status.HTTP_403_FORBIDDEN: "Forbidden",
    status.HTTP_404_NOT_FOUND: "Not found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed",
    status.HTTP_409_CONFLICT: "Conflict",
    status.HTTP_413_CONTENT_TOO_LARGE: "Payload too large",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Validation failed",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "Service unavailable",
}


# PLR0913: six keyword-only fields, because RFC 9457 has six fields. Grouping them into an
# object would add a type whose only job is to be unpacked one line later.
def problem_response(  # noqa: PLR0913
    *,
    status_code: int,
    detail: str,
    problem_type: str | None = None,
    title: str | None = None,
    errors: list[FieldError] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a problem+json response carrying the current request id."""
    slug = problem_type or _slug_for(status_code)
    problem = Problem(
        type=f"{PROBLEM_BASE_URI}/{slug}",
        title=title or _STATUS_TITLES.get(status_code, "Error"),
        status=status_code,
        detail=detail,
        request_id=get_request_id(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=problem.model_dump(exclude_none=True),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def _slug_for(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "error").lower().replace(" ", "-")


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler the app needs. Called once, by the app factory."""

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reshape FastAPI's validation errors into the one error format the hub uses.

        Only the location and the message are copied. FastAPI's raw errors include the
        offending **input**, and on this service a request body is something a person said
        to their assistant -- so the default handler would put a rejected message into a
        response, a client log and quite possibly an aggregator.
        """
        return problem_response(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the request failed validation",
            problem_type="validation-failed",
            errors=[
                FieldError(
                    location=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors()
            ],
        )

    @app.exception_handler(AuthenticationError)
    async def _auth(request: Request, exc: AuthenticationError) -> JSONResponse:
        headers = None
        container = getattr(request.app.state, "container", None)
        if container is not None:
            settings = container.settings
            headers = {
                "WWW-Authenticate": bearer_challenge(
                    settings.host, settings.port, settings.audience
                )
            }
        return problem_response(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            problem_type="unauthorized",
            headers=headers,
        )

    @app.exception_handler(KeyringUnreachableError)
    async def _keys(_request: Request, exc: KeyringUnreachableError) -> JSONResponse:
        # 503, never 401. The token is probably fine; keyring is not.
        return problem_response(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
            problem_type="keyring-unreachable",
            headers={RETRY_AFTER_HEADER: RETRY_AFTER_SECONDS},
        )

    @app.exception_handler(DeviceFlowError)
    async def _device(_request: Request, exc: DeviceFlowError) -> JSONResponse:
        """RFC 8628 clients branch on ``error``; a problem document is not that protocol."""
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": exc.code, "error_description": str(exc)},
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(LucyError)
    async def _domain(_request: Request, exc: LucyError) -> JSONResponse:
        """Render a domain failure at the status and slug the error itself declares.

        Annotating the argument as the exception it is registered for rather than as
        ``Exception`` is deliberate: the alternative is an ``isinstance`` narrowing whose
        false arm Starlette can never take, and an untestable branch in an error handler is
        worse than no branch at all.
        """
        return problem_response(status_code=exc.status, detail=str(exc), problem_type=exc.code)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Starlette's own refusals: an unrouted path, a method a route does not have."""
        return problem_response(status_code=exc.status_code, detail=str(exc.detail))


def unhandled_problem_response(exc: BaseException) -> JSONResponse:
    """Render an unexpected exception as a 500.

    The exception's own message is withheld: it can carry filesystem paths, internal
    hostnames, or a fragment of a conversation. The request id ties the response to the log
    record that does have the detail -- which is why this is invoked from inside
    :class:`~lucy_api.api.middleware.RequestContextMiddleware`, while the id is still bound,
    rather than from Starlette's outermost error middleware, where the binding has already
    unwound and the response would carry no id at all.

    Only the exception's **type name** is logged, never its arguments: a ``ValueError``
    raised deep in a write path routinely carries the value in its message.
    """
    logger.error("unhandled_exception error_type=%s", type(exc).__name__)
    return problem_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="an unexpected error occurred; quote the request id when reporting it",
    )
