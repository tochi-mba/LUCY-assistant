"""FastAPI dependency wiring."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from hello_api.auth.verifier import AuthenticationError, VerifiedCaller
from hello_api.core.container import Container

bearer_scheme = HTTPBearer(auto_error=False)

MISSING_CREDENTIALS = "a keyring token is required"


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def get_current_caller(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> VerifiedCaller:
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)
    return await container.verifier.verify(credentials.credentials)


CurrentCallerDep = Annotated[VerifiedCaller, Depends(get_current_caller)]
