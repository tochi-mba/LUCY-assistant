"""Request-scoped cross-cutting behaviour.

One middleware, doing what belongs to a single span: give the request an id, keep that id
readable for as long as the request lasts, and put it on the way out so the caller can quote
it. The hub is the front door to ten services, so a failure a person sees here has to be
traceable to the one downstream call that caused it, and an id that only exists in a log
line nobody can find is not traceability.

The unhandled-exception case is caught *here* rather than left to Starlette's outermost
error middleware. That one runs after this binding has unwound, so the 500 it produces would
carry no request id -- and the request id is the only thing the 500 body asks the caller
for, because the body deliberately says nothing else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from lucy_api.api.errors import unhandled_problem_response
from lucy_api.core.request_id import bind_request_id, new_request_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"
MAX_SUPPLIED_REQUEST_ID = 64


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds a request id for the life of the request and echoes it on the response."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # An id supplied by the caller is honoured so one trace can span services -- a
        # request here is usually one leg of somebody else's request, and the hub fans one
        # turn out across several siblings. It is length-capped because it is echoed back
        # and ends up in log records.
        supplied = request.headers.get(REQUEST_ID_HEADER)
        request_id = supplied[:MAX_SUPPLIED_REQUEST_ID] if supplied else new_request_id()

        with bind_request_id(request_id):
            try:
                response = await call_next(request)
            except Exception as exc:
                response = unhandled_problem_response(exc)

        response.headers[REQUEST_ID_HEADER] = request_id
        return response
