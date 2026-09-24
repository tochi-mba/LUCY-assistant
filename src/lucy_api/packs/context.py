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
    from lucy_api.packs.probes import ProbeCache
    from lucy_api.permissions.gate import Grant
    from lucy_api.work import Registry

from lucy_api.decide import Decisions
from lucy_api.settings.policy import TurnPolicy


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

    timeout_seconds: float | None = None
    """How long to wait on this one call, when the default is the wrong question.

    The default is right for a sibling that is either up or down, and wrong for a sibling
    that was asked to do work: a caller that asks the sandbox to spend up to sixty seconds on
    a command and then waits ten for the answer has guaranteed its own failure, whatever the
    sandbox does. Every workspace command ever run did exactly that.

    `None` means the client's own figure, which is what almost every call wants.
    """

    repeatable: bool | None = None
    """Whether sending this twice does it once, where the method does not say so.

    `None` leaves it to the method (RFC 9110 section 9.2.2), which is right for every write.
    A read sent as a POST -- a search, a scrape, a lookup, because its question does not fit a
    query string -- says `True`, so a 5xx or a late answer costs a repeat, as a GET's would,
    rather than a failed step.
    """


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


class ChildRuntime(Protocol):
    """Runs one helper and delivers a parent message to its inbox."""

    async def prepare(  # noqa: PLR0913 - the brief is objective, role, resume and schema
        self,
        parent: PackContext,
        *,
        objective: str,
        role: str,
        resume_from: str = "",
        return_schema: str = "",
        guidance: str = "",
    ) -> tuple[str, int]: ...

    async def run(
        self,
        parent: PackContext,
        *,
        objective: str,
        role: str,
        agent_id: str = "",
        task_id: int = 0,
    ) -> dict[str, Any]: ...

    async def discard_setup(self, parent: PackContext, agent_id: str, task_id: int) -> None: ...

    async def send(self, parent: PackContext, agent_id: str, body: str) -> dict[str, Any]: ...

    async def reopen(
        self, parent: PackContext, agent_id: str, *, return_schema: str = ""
    ) -> dict[str, Any]: ...

    async def stopped(self, parent: PackContext) -> list[dict[str, Any]]: ...

    async def read_journal(self, parent: PackContext) -> dict[str, Any]: ...

    async def claim(self, parent: PackContext, task_id: str) -> dict[str, Any]: ...

    async def complete(self, parent: PackContext, task_id: str) -> dict[str, Any]: ...


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
    workspace_environment_id: str = ""
    workspace_path: str = ""
    permission_mode: str = "ask"
    incognito: bool = False
    max_subagent_turns: int = 8
    policy: TurnPolicy = field(default_factory=TurnPolicy)
    decide: Decisions = field(default_factory=Decisions)
    catalogue: Catalogue | None = None
    work: Registry | None = None
    child: ChildRuntime | None = None
    grants: dict[str, Grant] = field(default_factory=dict)
    bound_ids: list[str] = field(default_factory=list)
    """Capabilities `capabilities.use` asked for since the last plan ran, in the order asked.

    Drained by `Capabilities.execute` into the session's recency, where a bind takes effect:
    the plan schema and the executor are rebuilt from recency every round, so a capability
    bound in one plan is callable in the very next one, in the same turn.
    """
    limits: dict[str, asyncio.Semaphore] = field(default_factory=dict)
    probes: ProbeCache | None = None
    defaults: dict[str, object] = field(default_factory=dict)
    """Sibling knobs this turn may use when the model omitted them. Never secrets."""
    step_seconds: float = 10.0
    """How long one step may run in the plan being executed, as weftai enforces it.

    weftai applies one `stepTimeoutMs` to every step alike and has no per-operation figure.
    So an operation that waits has to fit inside it by itself: `work.wait` was asked for
    120 seconds inside a 30-second step and was cut off, three times in one turn, with
    "timed out after 30000ms. Narrow the query or raise the step timeout" -- advice that
    means nothing for a wait -- and a waiting `workspace.run` cut off that way lost the
    handle to the command it had started.
    """

    def within_step(self, seconds: float) -> float:
        """The longest an operation may wait inside this step, and answer before it ends."""
        return max(0.0, min(seconds, self.step_seconds - STEP_MARGIN_SECONDS))

    def limit(self, service: str, *, concurrent: int = 2) -> asyncio.Semaphore:
        """The gate for one service, created the first time somebody asks for it."""
        if service not in self.limits:
            self.limits[service] = asyncio.Semaphore(concurrent)
        return self.limits[service]


STEP_MARGIN_SECONDS = 2.0
"""How long before its step ends a wait gives up, so that saying "still running" fits too."""


__all__ = [
    "STEP_MARGIN_SECONDS",
    "Call",
    "ChildRuntime",
    "Http",
    "NoBrokerError",
    "PackContext",
    "SilentTokens",
    "TokenSource",
]
