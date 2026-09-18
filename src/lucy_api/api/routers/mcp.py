"""Streamable HTTP MCP endpoint.

``POST /mcp`` only. GET and DELETE are 405 by never being registered. Origin is checked
before the token, so a DNS-rebinding browser does not learn that a token was missing.
``Mcp-Session-Id`` is read so it can be ignored; it is never copied onto the response.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from lucy_api.api.dependencies import MISSING_CREDENTIALS, ActingAs, ContainerDep, bearer_scheme
from lucy_api.auth.verifier import AuthenticationError
from lucy_api.mcp.dispatch import handle
from lucy_api.mcp.handlers import McpCall
from lucy_api.mcp.origin import assert_origin

router = APIRouter(tags=["mcp"])


async def mcp_acting(
    request: Request,
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ActingAs:
    """Origin first, then the bearer. The order is the DNS-rebinding defence."""
    assert_origin(request.headers.get("origin"), container.settings)
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)
    caller = await container.verifier.verify(credentials.credentials)
    return ActingAs(caller=caller, token=credentials.credentials)


McpActingDep = Annotated[ActingAs, Depends(mcp_acting)]


@router.post(
    "/mcp",
    operation_id="mcp_endpoint",
    summary="JSON-RPC MCP endpoint",
    description=(
        "Streamable HTTP, revision 2026-07-28, with a dual-era initialize path for "
        "2025-11-25 clients. POST only. A mismatched Origin is 403. JSON-RPC errors use "
        "the spec's status codes; they are not problem+json documents."
    ),
)
async def mcp_endpoint(request: Request, acting: McpActingDep, container: ContainerDep) -> Response:
    """One JSON-RPC message in, one JSON object or an empty 202 out."""
    result = await handle(await request.body(), request.headers, McpCall(acting, container))
    if result.body is None:
        return Response(status_code=result.status)
    return JSONResponse(status_code=result.status, content=result.body)
