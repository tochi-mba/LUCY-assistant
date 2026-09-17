"""FastAPI dependency wiring.

The container comes off ``app.state``; the caller comes off a verified bearer token and
nowhere else. There is no header that names an account, and no query parameter that would
let one person ask about another. Reading somebody else's conversation is not forbidden by a
check somewhere -- it is inexpressible, because no route has anywhere to put another
person's id.

The idempotency key is a dependency rather than a hand-read header for the same reason the
selection model is a type: every write that must survive a retry declares it in its
signature, so adding one and forgetting the key is a change that does not compile rather
than a duplicate charge somebody notices later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from lucy_api.auth.verifier import AuthenticationError, VerifiedCaller
from lucy_api.core.container import Container
from lucy_api.sessions.sql_store import SessionStore

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


@dataclass(frozen=True, slots=True)
class ActingAs:
    """The verified person *and* the token that proved it.

    Packs mint downstream tokens from this one. The account id is already on ``caller``;
    the raw token is here because a ``VerifiedCaller`` is not enough to exchange: keyring
    wants the JWT itself, and Lucy must never store it on the session row.
    """

    caller: VerifiedCaller
    token: str

    @property
    def account_id(self) -> str:
        return self.caller.account_id


async def get_acting_as(
    container: ContainerDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ActingAs:
    if credentials is None:
        raise AuthenticationError(MISSING_CREDENTIALS)
    caller = await container.verifier.verify(credentials.credentials)
    return ActingAs(caller=caller, token=credentials.credentials)


ActingAsDep = Annotated[ActingAs, Depends(get_acting_as)]


def get_store(container: ContainerDep) -> SessionStore:
    return container.store


StoreDep = Annotated[SessionStore, Depends(get_store)]


@dataclass(frozen=True, slots=True)
class StreamCursor:
    """The two transport cursors an SSE client can present.

    They stay together because they describe one resumption decision.  Keeping the FastAPI
    extraction here also keeps route signatures to their actual dependencies rather than
    counting two headers as two pieces of session work.
    """

    starting_after: str | None
    last_event_id: str | None


def get_stream_cursor(
    starting_after: Annotated[str | None, Query()] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamCursor:
    """Collect resumable-stream cursors without choosing between them yet."""
    return StreamCursor(starting_after=starting_after, last_event_id=last_event_id)


StreamCursorDep = Annotated[StreamCursor, Depends(get_stream_cursor)]

IdempotencyKeyDep = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=256,
        description=(
            "Any string unique to this request. Retrying with the same key returns the "
            "original response instead of doing the work twice; reusing it with a different "
            "body is a 409."
        ),
    ),
]
