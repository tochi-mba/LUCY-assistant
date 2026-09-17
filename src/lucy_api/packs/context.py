"""What a capability is handed when it probes, lists or runs.

This is also the `ctx` weftai passes to every handler, which is why it is a dataclass with
no behaviour: it reaches `CollectionType.fields(ctx)` and the formatter, so anything
reachable from here can end up rendered into a label the model reads. **Nothing secret may
be reachable from this object.** There is a test that asserts it, and this comment is the
reason the test exists.

A token is therefore never a field. `tokens` is a *broker*: something that will mint a
short-lived, audience-scoped token when a handler asks for one, so the credential exists
for the duration of one outbound call and never sits in a structure the formatter can walk.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lucy_api.packs.base import Catalogue
    from lucy_api.work import Registry


class TokenSource(Protocol):
    """Mints a short-lived token for one audience, on behalf of one verified person."""

    async def token_for(self, audience: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Call:
    """One outbound request, described rather than spread across six parameters.

    `audience` is here and not optional: every call to a sibling carries a token Lucy
    minted for that specific audience, and naming it at the call site is what makes a
    forgotten one a type error rather than a 401 in production.
    """

    method: str
    url: str
    audience: str
    json: Any = None
    params: Mapping[str, Any] | None = None
    headers: Mapping[str, str] | None = None


class Http(Protocol):
    """The one way a capability reaches a sibling service."""

    async def request(self, call: Call) -> Any: ...

    async def request_response(self, call: Call) -> Any: ...


class NoBrokerError(Exception):
    """A capability asked for a token in a turn that has no broker.

    Named rather than a bare ``RuntimeError`` so a probe can tell "not configured" from
    "the code is broken", and so a log line never has to print an audience to explain it.
    """


class SilentTokens:
    """A token source that never mints. Used when a turn has no broker and no sibling call."""

    async def token_for(self, audience: str) -> str:  # noqa: ARG002 - TokenSource
        raise NoBrokerError


@dataclass(slots=True)
class PackContext:
    """One person, one profile, one session, for the life of one turn.

    `limits` is a semaphore per service rather than a global one. A slow job must not be
    able to starve every other capability of the run's parallelism, and weftai's own
    `maxParallel` is a plan-wide number that cannot express "two of these at once".

    `work` is the registry of everything still in flight for this session -- helpers, jobs
    and commands alike. It is here rather than in each pack that starts something long
    because there is one of them, and because "what is still running?" has to be answerable
    from a handler that did not start any of it.
    """

    account_id: str
    profile: str
    session_id: str
    http: Http
    tokens: TokenSource
    turn_id: str = ""
    agent_id: str = ""
    depth: int = 0
    workspace_path: str = ""
    permission_mode: str = "ask"
    incognito: bool = False
    catalogue: Catalogue | None = None
    work: Registry | None = None
    bound_ids: set[str] = field(default_factory=set)
    limits: dict[str, asyncio.Semaphore] = field(default_factory=dict)

    def limit(self, service: str, *, concurrent: int = 2) -> asyncio.Semaphore:
        """The gate for one service, created the first time somebody asks for it."""
        if service not in self.limits:
            self.limits[service] = asyncio.Semaphore(concurrent)
        return self.limits[service]


__all__ = ["Call", "Http", "NoBrokerError", "PackContext", "SilentTokens", "TokenSource"]
