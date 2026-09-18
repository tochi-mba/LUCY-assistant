"""Register, pin and forget external MCP servers.

Tools imported from a server are data. Lucy stores a digest of the listing so a later
change is a pin mismatch, not a silent update of what the model may call.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Response, status

from lucy_api.api.dependencies import ContainerDep, CurrentCallerDep
from lucy_api.api.schemas.mcp_servers import McpServerList, McpServerResource, RegisterMcpServer
from lucy_api.api.schemas.problem import Problem

router = APIRouter(prefix="/v1/mcp/servers", tags=["mcp"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_409_CONFLICT: _PROBLEM,
    status.HTTP_403_FORBIDDEN: _PROBLEM,
    status.HTTP_400_BAD_REQUEST: _PROBLEM,
    status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM,
}
ServerName = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")]


@router.get(
    "",
    operation_id="list_mcp_servers",
    summary="External MCP servers this account registered",
    responses=_ADDRESSED,
)
async def list_mcp_servers(caller: CurrentCallerDep, container: ContainerDep) -> McpServerList:
    rows = await container.mcp_servers.list(caller.account_id)
    return McpServerList(data=[McpServerResource.model_validate(row) for row in rows])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    operation_id="register_mcp_server",
    summary="Pin an external MCP server's tools",
    responses=_ADDRESSED,
    description=(
        "Lucy fetches tools/list over HTTPS, scrubs and hash-pins the listing, and stores "
        "it under mcp.<name>. Loopback and RFC 1918 destinations are refused. A later "
        "listing that does not match the digest is pin_mismatch, not a silent update."
    ),
)
async def register_mcp_server(
    body: RegisterMcpServer, caller: CurrentCallerDep, container: ContainerDep
) -> McpServerResource:
    row = await container.mcp_servers.register(caller.account_id, body.name, str(body.url))
    return McpServerResource.model_validate(row)


@router.get(
    "/{name}",
    operation_id="get_mcp_server",
    summary="One pinned MCP server",
    responses=_ADDRESSED,
)
async def get_mcp_server(
    name: ServerName, caller: CurrentCallerDep, container: ContainerDep
) -> McpServerResource:
    row = await container.mcp_servers.get(caller.account_id, name)
    return McpServerResource.model_validate(row)


@router.post(
    "/{name}/refresh",
    operation_id="refresh_mcp_server",
    summary="Re-list a pinned server and compare the digest",
    responses=_ADDRESSED,
)
async def refresh_mcp_server(
    name: ServerName, caller: CurrentCallerDep, container: ContainerDep
) -> McpServerResource:
    row = await container.mcp_servers.refresh(caller.account_id, name)
    return McpServerResource.model_validate(row)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_mcp_server",
    summary="Forget a pinned MCP server",
    responses=_ADDRESSED,
)
async def delete_mcp_server(
    name: ServerName, caller: CurrentCallerDep, container: ContainerDep
) -> Response:
    await container.mcp_servers.delete(caller.account_id, name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
