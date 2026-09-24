"""Connection status and subject-bound provider consent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, CurrentCallerDep
from lucy_api.api.schemas.connections import (
    AuthorizationResource,
    AuthorizationStatus,
    ConnectionList,
    ConnectionResource,
)
from lucy_api.api.schemas.problem import Problem
from lucy_api.auth.device import AUTHORIZATION_PENDING
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.keyring import PENDING
from lucy_api.core.container import PackRequest
from lucy_api.core.errors import LucyError, absent

router = APIRouter(tags=["connections"])

_PROBLEM: dict[str, Any] = {"model": Problem}
CONNECTIONS_UNAVAILABLE = "connections-unavailable"
CONNECTION_UNAVAILABLE = "connection-unavailable"
_AUTHED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_503_SERVICE_UNAVAILABLE: _PROBLEM,
}
ServicePath = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]*$")]
TicketPath = Annotated[str, Path(min_length=1, max_length=128)]
ProfileQuery = Annotated[str, Query(min_length=1, max_length=128)]


def _forget_probes(container: object, account_id: str, profile: str) -> None:
    """A connect or disconnect must not leave a stale availability in the probe cache."""
    capabilities = getattr(container, "capabilities", None)
    forget = getattr(capabilities, "forget_probes", None)
    if callable(forget):
        forget(account_id, profile)


def _request(acting: ActingAsDep, profile: str) -> PackRequest:
    return PackRequest(
        caller=acting.caller,
        user_token=acting.token,
        profile=profile,
        session_id="",
    )


async def _connections(
    acting: ActingAsDep, container: ContainerDep, profile: str
) -> tuple[Any, ...]:
    try:
        return await container.connection_client(_request(acting, profile)).connections(profile)
    except DownstreamError as exc:
        message = "Connection state could not be read; try again after Keyring is healthy."
        raise LucyError(CONNECTIONS_UNAVAILABLE, message, 503) from exc


def _resource(connection: Any) -> ConnectionResource:
    return ConnectionResource(
        service=connection.service,
        status=connection.status,
        scopes=list(connection.scopes),
        expires_at=connection.expires_at,
        last_error=connection.last_error,
    )


@router.get(
    "/v1/connections",
    operation_id="list_connections",
    summary="List connection state for one profile",
    response_model=ConnectionList,
    responses=_AUTHED,
    description="Status, expiry and granted scopes only. No credential can appear here.",
)
async def list_connections(
    acting: ActingAsDep,
    container: ContainerDep,
    profile: ProfileQuery = "personal",
) -> ConnectionList:
    return ConnectionList(
        data=[_resource(item) for item in await _connections(acting, container, profile)]
    )


@router.get(
    "/v1/connections/{service}",
    operation_id="get_connection",
    summary="Read one connection",
    response_model=ConnectionResource,
    responses=_AUTHED,
    description="Returns 404 for both an absent connection and one outside this account.",
)
async def get_connection(
    service: ServicePath,
    acting: ActingAsDep,
    container: ContainerDep,
    profile: ProfileQuery = "personal",
) -> ConnectionResource:
    connection = next(
        (
            item
            for item in await _connections(acting, container, profile)
            if item.service == service
        ),
        None,
    )
    if connection is None:
        raise absent()
    return _resource(connection)


@router.post(
    "/v1/connections/{service}/authorize",
    operation_id="authorize_connection",
    summary="Start a browser connection",
    response_model=AuthorizationResource,
    responses=_AUTHED,
    description=(
        "Returns a short-lived link on Lucy's origin. Lucy verifies the browser subject "
        "again before redirecting to the provider."
    ),
)
async def authorize_connection(
    service: ServicePath,
    acting: ActingAsDep,
    container: ContainerDep,
    request: Request,
    profile: ProfileQuery = "personal",
) -> AuthorizationResource:
    try:
        authorization = await container.connection_client(_request(acting, profile)).authorize(
            profile, service
        )
    except DownstreamError as exc:
        message = "The connection could not be started; check its deployment and try again."
        raise LucyError(CONNECTION_UNAVAILABLE, message, 503) from exc
    ticket = container.connection_tickets.create(acting.account_id, profile, service, authorization)
    _forget_probes(container, acting.account_id, profile)
    origin = str(request.base_url).rstrip("/")
    query = urlencode({"ticket": ticket.id})
    return AuthorizationResource(
        connect_url=f"{origin}/connect?{query}",
        ticket=ticket.id,
        expires_at=datetime.fromtimestamp(ticket.expires_at, UTC),
        poll_url=f"{origin}/v1/connections/{service}/authorize/{ticket.id}",
        interval=5,
    )


@router.get(
    "/v1/connections/{service}/authorize/{ticket}",
    operation_id="poll_connection_authorization",
    summary="Check a browser connection",
    response_model=AuthorizationStatus,
    responses=_AUTHED,
    description="Uses RFC 8628-style pending and terminal state words without returning tokens.",
)
async def poll_connection_authorization(
    service: ServicePath,
    ticket: TicketPath,
    acting: ActingAsDep,
    container: ContainerDep,
) -> AuthorizationStatus:
    record = container.connection_tickets.read(ticket, acting.account_id)
    if record.service != service:
        raise absent()
    connection = next(
        (
            item
            for item in await _connections(acting, container, record.profile)
            if item.service == service
        ),
        None,
    )
    # keyring's own placeholder is not an answer: it is written before the consent link is
    # handed out, so a poll that passed it through never said the advertised word once.
    if connection is None or connection.status == PENDING:
        word = AUTHORIZATION_PENDING
    else:
        word = connection.status
        _forget_probes(container, acting.account_id, record.profile)
    return AuthorizationStatus(
        service=service,
        profile=record.profile,
        status=word,
        expires_at=datetime.fromtimestamp(record.expires_at, UTC),
    )


@router.delete(
    "/v1/connections/{service}",
    operation_id="delete_connection",
    summary="Disconnect one provider account",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_AUTHED,
    description="Idempotently removes the stored connection for this person and profile.",
)
async def delete_connection(
    service: ServicePath,
    acting: ActingAsDep,
    container: ContainerDep,
    profile: ProfileQuery = "personal",
) -> Response:
    try:
        await container.connection_client(_request(acting, profile)).disconnect(profile, service)
    except DownstreamError as exc:
        message = "The connection could not be removed; try again after Keyring is healthy."
        raise LucyError(CONNECTION_UNAVAILABLE, message, 503) from exc
    _forget_probes(container, acting.account_id, profile)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/connect",
    operation_id="open_connection_ticket",
    summary="Verify the browser subject and continue to provider consent",
    responses=_AUTHED,
    include_in_schema=False,
)
async def open_connection_ticket(
    ticket: Annotated[str, Query(min_length=1, max_length=128)],
    caller: CurrentCallerDep,
    container: ContainerDep,
) -> RedirectResponse:
    provider_url = container.connection_tickets.open(ticket, caller.account_id)
    record = container.connection_tickets.read(ticket, caller.account_id)
    _forget_probes(container, caller.account_id, record.profile)
    return RedirectResponse(provider_url, status_code=status.HTTP_303_SEE_OTHER)


__all__ = ["router"]
