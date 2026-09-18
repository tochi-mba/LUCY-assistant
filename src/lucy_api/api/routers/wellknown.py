"""Unauthenticated discovery documents for clients that speak OAuth.

A bearer-protected API that does not advertise where tokens come from leaves every client
to hard-code an issuer. This document is that advertisement, and it is public on purpose:
it names Lucy as a resource and Keyring as the authorization server, and it never names a
person.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from lucy_api.api.dependencies import ContainerDep

router = APIRouter(tags=["discovery"])


@router.get(
    "/.well-known/oauth-protected-resource",
    operation_id="oauth_protected_resource",
    summary="Where this API's tokens come from",
    description=(
        "RFC 9728 resource metadata. Unauthenticated, because a client has to read it "
        "before it has a token."
    ),
)
async def oauth_protected_resource(container: ContainerDep) -> dict[str, Any]:
    settings = container.settings
    return {
        "resource": f"http://{settings.host}:{settings.port}",
        "authorization_servers": [settings.keyring_issuer],
        "bearer_methods_supported": ["header"],
        "resource_name": settings.app_name,
    }
