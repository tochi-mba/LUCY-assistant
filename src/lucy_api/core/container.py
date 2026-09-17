"""The process-scoped object graph.

Everything with a lifetime longer than a request is built here once, in a FastAPI lifespan,
and hung on ``app.state.container``. Constructing the container does no network I/O, which is
what lets the whole hub be exercised in-process against a fake keyring by passing a
``transport``.

The database is the exception to "no I/O": the worker opens its connection on its own thread
as soon as it is constructed, so a database that cannot be opened surfaces on the first call
rather than at import time. The schema is created by :meth:`Container.start`, which the
lifespan awaits, because it is the one piece of setup that genuinely must finish before the
first request rather than on first use.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
from keyring_client import JwksClient, SystemClock

from lucy_api.auth.broker import Delegation, TokenBroker, TokenCache
from lucy_api.auth.exchange import KeyringExchange
from lucy_api.auth.verifier import TokenVerifier, VerifiedCaller
from lucy_api.model.registry import ModelRegistry, http_registry
from lucy_api.packs.http import PackHttp
from lucy_api.packs.service import Capabilities, installed_packs
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.snapshot import SessionSnapshotter
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import TurnSupervisor

if TYPE_CHECKING:
    from lucy_api.core.config import Settings
    from lucy_api.packs.context import PackContext


@dataclass(frozen=True, slots=True)
class PackRequest:
    """Who is asking, and which session they are asking about.

    The token travels with the caller because a verified subject is not enough to
    exchange: keyring wants the JWT itself, and Lucy must never write that JWT onto a
    session row.
    """

    caller: VerifiedCaller
    user_token: str
    profile: str
    session_id: str
    turn_id: str = ""
    permission_mode: str = "ask"
    incognito: bool = False


@dataclass(slots=True)
class Container:
    """What every request shares for the life of the process."""

    settings: Settings
    jwks: JwksClient
    verifier: TokenVerifier
    worker: SqlWorker
    store: SessionStore
    events: EventEmitter
    models: ModelRegistry
    capabilities: Capabilities
    turns: TurnSupervisor
    exchange: KeyringExchange
    token_cache: TokenCache
    outbound: httpx.AsyncClient
    started_at: float = field(default_factory=time.monotonic)

    def pack_context(self, request: PackRequest) -> PackContext:
        """The seam a request uses to talk to siblings: minted tokens, never the caller's.

        Built per request because the person is per request. The cache and the connection
        pool are process-scoped, so two turns for the same person reuse a live token and
        the same outbound client without either request closing it.

        With no service token Lucy cannot exchange, so the packs see an empty seam rather
        than a client that would hang trying to mint. That is a deployment that is not
        ready to act for anyone, not a per-request failure.
        """
        if not self.settings.keyring_service_token.strip():
            return self.capabilities.context_for(
                SessionScope(
                    account_id=request.caller.account_id,
                    profile=request.profile,
                    session_id=request.session_id,
                    turn_id=request.turn_id,
                    permission_mode=request.permission_mode,
                    incognito=request.incognito,
                )
            )
        broker = TokenBroker(
            exchange=self.exchange,
            delegation=Delegation.for_person(request.caller, user_token=request.user_token),
            cache=self.token_cache,
        )
        http = PackHttp(
            tokens=broker,
            timeout_seconds=self.settings.http_timeout_seconds,
            client=self.outbound,
        )
        return self.capabilities.context_for(
            SessionScope(
                account_id=request.caller.account_id,
                profile=request.profile,
                session_id=request.session_id,
                turn_id=request.turn_id,
                permission_mode=request.permission_mode,
                incognito=request.incognito,
            ),
            http=http,
            tokens=broker,
        )

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    async def start(self) -> None:
        """Bring the database up to the current schema before anything is served."""
        await self.store.initialize()
        await self.turns.start()

    async def aclose(self) -> None:
        """Release both long-lived resources, even if the first one objects.

        The worker's thread is retired whatever the JWKS client does on the way out. A leaked
        thread holds an open SQLite connection, and on a file-backed database that means a
        WAL nothing ever checkpoints.
        """
        try:
            await self.turns.aclose()
        finally:
            try:
                await self.models.aclose()
            finally:
                try:
                    await self.jwks.aclose()
                finally:
                    try:
                        await self.exchange.aclose()
                    finally:
                        try:
                            await self.outbound.aclose()
                        finally:
                            await self.worker.aclose()


def build_container(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> Container:
    """Assemble the graph. No network I/O happens here, only on first use."""
    clock = SystemClock()
    jwks = JwksClient(
        url=settings.keyring_jwks_url,
        clock=clock,
        cache_seconds=settings.jwks_cache_seconds,
        min_refetch_seconds=settings.jwks_min_refetch_seconds,
        timeout_seconds=settings.http_timeout_seconds,
        transport=transport,
    )
    verifier = TokenVerifier(
        jwks=jwks,
        issuer=settings.keyring_issuer,
        audience=settings.audience,
        clock=clock,
    )
    worker = SqlWorker(settings.database_path)
    store = SessionStore(worker)
    events = EventEmitter(SqlEventLog(store), SessionSnapshotter(store))
    models = http_registry(
        {"anthropic": settings.anthropic_api_key, "openai": settings.openai_api_key},
        transport=transport,
        timeout=settings.http_timeout_seconds,
    )
    capabilities = Capabilities(installed_packs(memory_base_url=settings.memory_api_base_url))
    exchange = KeyringExchange(
        base_url=settings.keyring_base_url,
        service_token=settings.keyring_service_token,
        timeout_seconds=min(settings.http_timeout_seconds, 5.0),
        transport=transport,
    )
    outbound = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        transport=transport,
        follow_redirects=False,
    )
    return Container(
        settings=settings,
        jwks=jwks,
        verifier=verifier,
        worker=worker,
        store=store,
        events=events,
        models=models,
        capabilities=capabilities,
        turns=TurnSupervisor(store, models, events, capabilities),
        exchange=exchange,
        token_cache=TokenCache(),
        outbound=outbound,
    )


__all__ = ["Container", "PackRequest", "build_container"]
