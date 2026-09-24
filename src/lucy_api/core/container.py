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

import asyncio
import contextlib
import hashlib
import logging
import posixpath
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from keyring_client import JwksClient, SystemClock
from settings_client import HttpSettingsClient
from settings_client.errors import SettingsRejected, SettingsUnavailable

from lucy_api.agents.journal import JournalLive
from lucy_api.agents.runtime import ChildRuntime
from lucy_api.agents.store import AgentStore
from lucy_api.auth.broker import Delegation, TokenBroker, TokenCache
from lucy_api.auth.device import DeviceFlow
from lucy_api.auth.exchange import KeyringExchange
from lucy_api.auth.verifier import TokenVerifier, VerifiedCaller
from lucy_api.blobs import Blobs
from lucy_api.clients.environments import HttpEnvironmentsClient
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.keyring import DelegatedKeyringClient
from lucy_api.clients.live_feeds import (
    MusicFeeds,
    PersonaFeeds,
    ResearchFeeds,
    UserFeeds,
    WorkspaceFeeds,
)
from lucy_api.clients.memory import AUDIENCE as MEMORY_AUDIENCE
from lucy_api.clients.memory import HttpMemoryClient
from lucy_api.clients.spotify import HttpSpotifyClient
from lucy_api.clients.user import HttpUserClient
from lucy_api.connections.tickets import ConnectionTickets
from lucy_api.context.build import Live
from lucy_api.context.fields import FIELDS, feed_setting_key
from lucy_api.context.policy import ALLOW_UNKNOWN, HIDE_PERSONAL, MASTER, ExplicitFlags
from lucy_api.context.sources import Sources
from lucy_api.core.errors import LucyError, settings_unavailable
from lucy_api.mcp.outbound import httpx_call, httpx_listing
from lucy_api.mcp.servers import McpServers
from lucy_api.memory.index import MemoryIndex
from lucy_api.model.readiness import Readiness
from lucy_api.model.registry import ModelRegistry, http_registry
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportDownstreamError
from lucy_api.packs.http import PackHttp, apply_downstream_policy
from lucy_api.packs.mcp import McpPack
from lucy_api.packs.probes import GuardedHttp
from lucy_api.packs.service import Capabilities, installed_packs
from lucy_api.packs.watch import WatchPack, httpx_fetch
from lucy_api.permissions.live import PendingLive
from lucy_api.sessions.models import TERMINAL
from lucy_api.sessions.scope import (
    GIT_BASELINE,
    GIT_INIT,
    PROGRESS_FILE,
    PROGRESS_STARTER,
    TASKS_FILE,
    TASKS_STARTER,
    SessionScope,
    WorkspaceScope,
    disabled_in,
)
from lucy_api.sessions.snapshot import SessionSnapshotter
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.settings.policy import SETTINGS_UNAVAILABLE, TurnPolicy
from lucy_api.store.worker import SqlWorker
from lucy_api.stream.emitter import EventEmitter, SqlEventLog
from lucy_api.turn.supervisor import PreparedTurn, TurnSupervisor
from lucy_api.webhooks import Webhooks, httpx_deliver
from lucy_api.work.live import WorkInFlight
from lucy_api.work.registry import Registry as WorkRegistry
from lucy_api.work.wake import Waker
from lucy_api.workspace.orient import WorkspaceLive

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from settings_client import ResolvedSettings, SettingsClient

    from lucy_api.clients.environments import EnvironmentsClient
    from lucy_api.context.feeds import FeedSource
    from lucy_api.core.config import Settings
    from lucy_api.memory.index import TopicListing
    from lucy_api.packs.context import PackContext
    from lucy_api.permissions.gate import Grant
    from lucy_api.sessions.models import CreateSession
    from lucy_api.turn.stop import Budget as TurnBudget


logger = logging.getLogger(__name__)

OWN_NAMESPACE = "lucy"
"""The hub's own settings: the turn policy, and everything `TurnPolicy` clamps."""

SEARCH_NAMESPACE = "search"
"""web-search's namespace, read for the person's backend and result count."""

MUSIC_NAMESPACE = "spotify"
"""spotify's namespace, read for the person's default playback device."""

SIBLING_NAMESPACES = (SEARCH_NAMESPACE, MUSIC_NAMESPACE)
"""Namespaces owned by a sibling that the hub nonetheless resolves.

Named rather than spelled inline at the call site, because settings-api grants namespaces
per service and a namespace the hub reads but was not granted answers 403 -- which
``_optional_namespace`` swallows on purpose, since a settings outage must not take a turn
down. A missing *grant* is not an outage, and looks exactly like one from here. Keeping
the list in one place is what lets `tests/hub/test_settings_namespaces.py` check the grants
in `scripts/genenv.py` against it.
"""

NAMESPACES_READ = (OWN_NAMESPACE, *SIBLING_NAMESPACES)
"""Every namespace the hub resolves, and so every namespace it must be granted."""


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
    readiness: Readiness
    capabilities: Capabilities
    turns: TurnSupervisor
    exchange: KeyringExchange
    token_cache: TokenCache
    outbound: httpx.AsyncClient
    preferences: SettingsClient
    connection_tickets: ConnectionTickets
    device_flow: DeviceFlow
    work: WorkRegistry
    agents: AgentStore
    mcp_servers: McpServers
    blobs: Blobs
    webhooks: Webhooks
    environment_override: EnvironmentsClient | None = None
    memory_topics: TopicListing | None = None
    started_at: float = field(default_factory=time.monotonic)
    _workspace_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

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
        http = GuardedHttp(
            PackHttp(
                tokens=broker,
                timeout_seconds=self.settings.http_timeout_seconds,
                client=self.outbound,
                service_tokens=_sibling_service_tokens(self.settings),
            ),
            self.capabilities.providers,
            account_id=request.caller.account_id,
            profile=request.profile,
            on_disconnect=lambda: self.capabilities.forget_probes(
                request.caller.account_id, request.profile
            ),
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

    def connection_client(self, request: PackRequest) -> DelegatedKeyringClient:
        """Manage connection metadata through Keyring's two-credential internal surface."""
        return DelegatedKeyringClient(
            self.outbound,
            self.settings.keyring_base_url,
            service_token=self.settings.keyring_service_token,
            user_token=request.user_token,
        )

    def environment_client(self, request: PackRequest) -> EnvironmentsClient:
        """The delegated workspace client, replaceable by an in-memory one in tests."""
        if self.environment_override is not None:
            return self.environment_override
        context = self.pack_context(request)
        return HttpEnvironmentsClient(context.http, self.settings.environments_api_base_url)

    async def ensure_workspace(
        self, request: PackRequest, session: dict[str, object]
    ) -> dict[str, object]:
        """Provision and durably attach the isolated workspace for one session.

        One environment belongs to the account/profile and each session receives its own
        confined directory inside it. Listing before create makes a retry after a process
        interruption recover that environment rather than consume another quota slot.
        """
        if session.get("workspace_environment_id"):
            return session
        session_id = str(session["id"])
        name = _workspace_name(request.caller.account_id, request.profile)
        lock = self._workspace_locks.setdefault(name, asyncio.Lock())
        async with lock:
            current = await self.store.get(request.caller.account_id, session_id)
            if current.get("workspace_environment_id"):
                return current
            client = self.environment_client(request)
            available = await client.environments(profile=request.profile)
            existing = next((item for item in available if item.name == name), None)
            environment = existing or await client.create(name, profile=request.profile)
            workspace = WorkspaceScope(environment.environment_id, session_id)
            await client.mkdir(environment.environment_id, workspace.root)
            await _bootstrap_session_workspace(client, workspace)
            return await self.store.attach_workspace(
                request.caller.account_id,
                session_id,
                environment.environment_id,
                workspace.root,
            )

    def workspace_view(self, session: dict[str, object]) -> dict[str, object]:
        """What a client may know: an environment id and a relative path, never a host path."""
        env_id = str(session.get("workspace_environment_id") or "")
        rel = str(session.get("workspace_rel") or "")
        return {
            "environment_id": env_id or None,
            "path": rel,
            "status": "attached" if env_id and rel else "missing",
        }

    async def reset_workspace(self, request: PackRequest, session_id: str) -> dict[str, object]:
        """Wipe the session subtree and seed it again. The environment itself stays."""
        row = await self.store.get(request.caller.account_id, session_id)
        if not row.get("workspace_environment_id"):
            row = await self.ensure_workspace(request, row)
        env_id = str(row.get("workspace_environment_id") or "")
        recorded = str(row.get("workspace_rel") or "")
        rel = recorded or WorkspaceScope(env_id, session_id).root
        if env_id and not recorded:

            def restore(db: Any) -> None:
                db.execute(
                    "UPDATE sessions SET workspace_rel=? WHERE id=?",
                    (rel, session_id),
                )

            await self.store.transaction(restore)
            row = {**row, "workspace_rel": rel}
        client = self.environment_client(request)
        with contextlib.suppress(DownstreamError, KeyError, LucyError):
            await client.delete(env_id, rel, recursive=True)
        await client.mkdir(env_id, rel)
        await _bootstrap_session_workspace(client, WorkspaceScope(env_id, session_id))
        return self.workspace_view(row)

    async def session_memory(
        self, request: PackRequest, session: dict[str, object]
    ) -> dict[str, object]:
        """The topic index currently eligible for this conversation's prompt."""
        if session.get("incognito"):
            return {"data": [], "incognito": True}
        listing = self.memory_topics
        if listing is None:
            listing = HttpMemoryClient(
                self.pack_context(request).http, self.settings.memory_api_base_url
            )
        resolved = await self._turn_settings(request.user_token, request.profile)
        policy = TurnPolicy.from_resolved(resolved)
        index = MemoryIndex(
            listing,
            profile=request.profile,
            limit=policy.memory_retrieval_limit,
            incognito=False,
        )
        try:
            snapshots = await index.fetch(request.session_id)
        except (DownstreamError, TransportDownstreamError, LucyError, NoBrokerError):
            return {"data": [], "incognito": False, "notice": "memory is unavailable"}
        return {
            "data": [
                {
                    "id": topic.id,
                    "title": topic.title,
                    "summary": topic.summary,
                    "count": topic.count,
                    "trust": topic.trust,
                    "unread": topic.unread,
                    "last_seen": None if topic.last_seen is None else topic.last_seen.isoformat(),
                }
                for topic in snapshots
            ],
            "incognito": False,
        }

    async def discard_session(self, request: PackRequest, session_id: str) -> None:
        """Drop the session, its artifacts, and its confined workspace subtree.

        The account's environment is shared across conversations, so this deletes the
        session directory rather than the sandbox. Memory is not this service's to erase.
        """
        row = await self.store.get(request.caller.account_id, session_id)
        await self.blobs.delete_session(request.caller.account_id, session_id)
        env_id = str(row.get("workspace_environment_id") or "")
        rel = str(row.get("workspace_rel") or "")
        await self.store.delete(request.caller.account_id, session_id)
        if not env_id or not rel:
            return
        bound = PackRequest(
            caller=request.caller,
            user_token=request.user_token,
            profile=str(row["profile"]),
            session_id=session_id,
        )
        try:
            await self.environment_client(bound).delete(env_id, rel, recursive=True)
        except (DownstreamError, KeyError, LucyError):
            return

    async def erase_account(self, request: PackRequest) -> None:
        """Wipe Lucy's copy of this person. Memory-api is a different store and is left."""
        workspaces = await self.blobs.erase_account(request.caller.account_id)
        client = self.environment_client(request)
        for env_id, rel in workspaces:
            try:
                await client.delete(env_id, rel, recursive=True)
            except (DownstreamError, KeyError, LucyError):
                continue

    async def apply_create_defaults(self, request: CreateSession, user_token: str) -> CreateSession:
        """Fill omitted create fields from this person's lucy settings.

        The session row is the live override after this. A later PATCH still wins. An
        omitted model is not the CreateSession constructor default leaking through as if
        the person chose it.
        """
        resolved = await self._turn_settings(user_token, request.profile)
        policy = TurnPolicy.from_resolved(resolved)
        sent = request.model_fields_set
        updates: dict[str, object] = {}
        if "model" not in sent:
            updates["model"] = policy.model
        if "thinking_config" not in sent:
            updates["thinking_config"] = policy.thinking
        if "permission_mode" not in sent:
            updates["permission_mode"] = policy.permission_mode
        if "input_policy" not in sent:
            updates["input_policy"] = policy.input_policy
        if "incognito" not in sent:
            updates["incognito"] = policy.incognito
        if not updates:
            return request
        return request.model_copy(update=updates)

    async def prepare_turn(self, request: PackRequest, session: dict[str, object]) -> PreparedTurn:
        """Resolve one turn's ephemeral authority, feeds, and feed policy.

        The caller token is consumed here by the two clients that must prove a subject. It
        is never copied into the session, turn, event, or prompt and the returned brokers
        stay in memory only until the supervisor claims this turn.
        """
        resolved = await self._turn_settings(request.user_token, request.profile)
        policy = TurnPolicy.from_resolved(resolved)
        if policy.blocks_turn:
            raise settings_unavailable(SETTINGS_UNAVAILABLE)
        pack_context = self.pack_context(request)
        pack_context.policy = policy.for_session(disabled_in(session))
        pack_context.max_subagent_turns = policy.max_subagent_turns
        pack_context.defaults = await self._pack_defaults(request.user_token, request.profile)
        apply_downstream_policy(pack_context.http, policy)
        feeds: list[FeedSource] = [
            PersonaFeeds(pack_context.http, self.settings.persona_api_base_url),
            UserFeeds(HttpUserClient(pack_context.http, self.settings.user_api_base_url)),
            MusicFeeds(HttpSpotifyClient(pack_context.http, self.settings.spotify_api_base_url)),
            ResearchFeeds(str(pack_context.defaults.get("research.backend") or "")),
        ]
        environment_id = str(session.get("workspace_environment_id") or "")
        workspace_live = None
        if environment_id:
            workspace = WorkspaceScope(environment_id, request.session_id)
            pack_context.workspace_environment_id = environment_id
            pack_context.workspace_path = workspace.root
            workspace_live = WorkspaceLive(
                self.environment_client(request),
                workspace,
                retention_hours=policy.workspace_retention_hours,
            )
            feeds.append(
                WorkspaceFeeds(
                    HttpEnvironmentsClient(
                        pack_context.http, self.settings.environments_api_base_url
                    ),
                    environment_id,
                    workspace_rel=workspace.root,
                )
            )
        pack_context.grants = await _load_grants(
            self.store, request.caller.account_id, request.profile, request.session_id
        )
        sources = Sources(
            in_flight=WorkInFlight(self.work),
            tasks=JournalLive(self.agents, request.caller.account_id),
            workspace=workspace_live,
            topics=MemoryIndex(
                HttpMemoryClient(pack_context.http, self.settings.memory_api_base_url),
                profile=request.profile,
                limit=policy.memory_retrieval_limit,
                incognito=request.incognito,
            ),
            pending=PendingLive(
                store=self.store,
                tickets=self.connection_tickets,
                account_id=request.caller.account_id,
                profile=request.profile,
            ),
        )
        return PreparedTurn(
            pack_context=pack_context,
            live=Live(sources=sources, feeds=tuple(feeds), flags=_feed_flags(resolved)),
            budget=_budget_for(policy),
            max_subagent_turns=policy.max_subagent_turns,
        )

    async def lucy_policy(self, user_token: str, profile: str | None = None) -> TurnPolicy:
        """The lucy knobs for this person and profile, already clamped.

        Routers that are not running a turn still owe the person their own numbers --
        compact's keep-window is one. They must not call ``_turn_settings`` themselves.
        """
        return TurnPolicy.from_resolved(await self._turn_settings(user_token, profile))

    async def _pack_defaults(self, user_token: str, profile: str | None) -> dict[str, object]:
        """Sibling knobs the packs may use when the model omitted them. Never secrets."""
        defaults: dict[str, object] = {}
        search = await self._optional_namespace(SEARCH_NAMESPACE, user_token, profile)
        if search is not None:
            limit = search.get("default_result_count", 8)
            if isinstance(limit, int) and not isinstance(limit, bool):
                defaults["research.limit"] = min(20, max(1, limit))
            backend = search.get("search_backend", "google")
            if isinstance(backend, str) and backend:
                defaults["research.backend"] = backend
        music = await self._optional_namespace(MUSIC_NAMESPACE, user_token, profile)
        if music is not None:
            device = music.get("default_device", None)
            if isinstance(device, str) and device:
                defaults["music.device_id"] = device
        return defaults

    async def _optional_namespace(
        self, namespace: str, user_token: str, profile: str | None
    ) -> ResolvedSettings | None:
        """A sibling namespace, or nothing. An outage here must not take the turn down.

        A rejection is not an outage. 403 means this service was never granted the namespace,
        which no retry and no waiting will fix, and which looks identical from here to a
        settings-api that is merely down -- so it is logged once per turn rather than
        swallowed. It stays non-fatal: the person asked for something else, and losing a
        default result count is not worth losing the answer.
        """
        try:
            return await self.preferences.resolve(namespace, user_token=user_token, profile=profile)
        except SettingsUnavailable:
            return None
        except SettingsRejected:
            logger.warning(
                "settings_namespace_refused namespace=%s -- the hub is not granted it, so its"
                " defaults are being ignored; see SETTINGS_API_SERVICES",
                namespace,
            )
            return None

    async def _turn_settings(
        self, user_token: str, profile: str | None = None
    ) -> ResolvedSettings | None:
        """Resolve the Lucy namespace once for everything this turn reads from it.

        The profile goes with every call. A client that could not take one used to be
        tolerated with a fallback; it is not any more, because a resolve that silently
        drops the profile answers with another profile's values, which is worse than
        failing.
        """
        try:
            return await self.preferences.resolve(
                OWN_NAMESPACE, user_token=user_token, profile=profile
            )
        except SettingsUnavailable:
            return None

    @property
    def uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at

    async def start(self) -> None:
        """Bring the database up to the current schema before anything is served."""
        await self.store.initialize()
        await self.turns.start()

    async def aclose(self) -> None:
        """Release every long-lived resource, even if an earlier close objects."""
        try:
            await self.work.shutdown()
        finally:
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
                                try:
                                    await self.preferences.aclose()
                                finally:
                                    try:
                                        self.blobs.close()
                                    finally:
                                        await self.worker.aclose()


def _feed_flags(resolved: ResolvedSettings | None) -> ExplicitFlags:
    """This person's feed switches, or the catalogue's conservative defaults."""
    values: dict[str, bool] = {}
    if resolved is None:
        return ExplicitFlags(values)
    keys = {
        MASTER,
        HIDE_PERSONAL,
        ALLOW_UNKNOWN,
        *(feed_setting_key(field.capability) for field in FIELDS),
        *(field.setting_key for field in FIELDS),
    }
    for key in keys:
        value = resolved.get(key, None)
        if isinstance(value, bool):
            values[key] = value
    return ExplicitFlags(values)


def _budget_for(policy: TurnPolicy) -> TurnBudget:
    """The loop ceilings, taken from the policy already clamped at the consuming edge."""
    from lucy_api.turn.stop import Budget  # noqa: PLC0415 - avoids a composition-root cycle

    return Budget(
        max_iterations=policy.max_llm_turns,
        max_tool_calls=policy.max_tool_calls,
        max_seconds=float(policy.max_turn_seconds),
        max_tokens=policy.session_token_budget,
    )


async def _load_grants(
    store: SessionStore, account: str, profile: str, session_id: str = ""
) -> dict[str, Grant]:
    from lucy_api.permissions.store import grants_for  # noqa: PLC0415 - keeps the root acyclic

    return dict(await grants_for(store, account, profile, session_id=session_id))


def _integer(
    resolved: ResolvedSettings | None,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if resolved is None:
        return default
    value = resolved.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(maximum, max(minimum, value))


def _workspace_name(account: str, profile: str) -> str:
    """A non-identifying, valid and stable environment name for one profile."""
    digest = hashlib.sha256(f"{account}\0{profile}".encode()).hexdigest()[:20]
    return f"lucy-{digest}"


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
    # The model registry is validated before the database is opened: a typo in a model
    # key is a startup error, and a startup error must not leave a worker thread behind.
    models = http_registry(
        settings.api_keys(),
        base_urls=settings.model_base_urls,
        transport=transport,
        timeout=settings.model_timeout_seconds,
    )
    worker = SqlWorker(settings.database_path)
    store = SessionStore(worker)
    events = EventEmitter(SqlEventLog(store), SessionSnapshotter(store))
    work = WorkRegistry(now=lambda: datetime.now(UTC))
    outbound = httpx.AsyncClient(
        timeout=settings.http_timeout_seconds,
        transport=transport,
        follow_redirects=False,
    )
    mcp_servers = McpServers(store, httpx_listing(outbound))
    webhooks = Webhooks(store, deliver=httpx_deliver(outbound))
    capabilities = Capabilities(
        (
            *installed_packs(
                memory_base_url=settings.memory_api_base_url,
                user_base_url=settings.user_api_base_url,
                spotify_base_url=settings.spotify_api_base_url,
                search_base_url=settings.web_search_base_url,
                settings_base_url=settings.settings_api_base_url,
                environments_base_url=settings.environments_api_base_url,
            ),
            WatchPack(settings.environments_api_base_url, fetch=httpx_fetch(outbound)),
            McpPack(mcp_servers, httpx_call(outbound)),
        ),
        work=work,
    )
    agents = AgentStore(store)
    capabilities.child = ChildRuntime(store, agents, models, capabilities)
    # Every ending in the registry becomes an event, and -- for work that asked -- a turn.
    # The waker needs the supervisor to start that turn and the supervisor needs the waker
    # to spend endings held while a turn ran, so the two are introduced after both exist.
    waker = Waker(store, events)
    work.on_finished(waker.on_finished)
    turns = TurnSupervisor(
        store,
        models,
        events,
        capabilities,
        agents=agents,
        on_status=after_turn(webhooks.notify, waker, store),
    )
    waker.attach(turns.wake)
    exchange = KeyringExchange(
        base_url=settings.keyring_base_url,
        service_token=settings.keyring_service_token,
        timeout_seconds=min(settings.http_timeout_seconds, 5.0),
        transport=transport,
    )
    preferences = HttpSettingsClient(
        base_url=settings.settings_api_base_url,
        service_token=settings.settings_api_token,
        timeout_seconds=min(settings.http_timeout_seconds, 5.0),
        transport=transport,
    )
    return Container(
        settings=settings,
        jwks=jwks,
        verifier=verifier,
        worker=worker,
        store=store,
        events=events,
        models=models,
        readiness=Readiness(settings.api_keys(), settings.model_base_urls, transport=transport),
        capabilities=capabilities,
        turns=turns,
        exchange=exchange,
        token_cache=TokenCache(),
        outbound=outbound,
        preferences=preferences,
        connection_tickets=ConnectionTickets(),
        device_flow=DeviceFlow(worker),
        work=work,
        agents=agents,
        mcp_servers=mcp_servers,
        blobs=Blobs(store, root=_blobs_root(settings)),
        webhooks=webhooks,
    )


def after_turn(
    notify: Callable[[str, str, str, str], Awaitable[None]],
    waker: Waker,
    store: SessionStore | None = None,
) -> Callable[[str, str, str, str], Awaitable[None]]:
    """What runs when a turn ends: the webhook fan-out, held changes, then held wakes.

    In that order. The webhook says the turn ended; a change held for "after this turn"
    lands next, so that a wake -- which may start the next turn -- runs under the settings
    the person asked for. A parked turn (`input_required`) is not an ending, so nothing
    held is spent for it.
    """

    async def ended(account_id: str, session_id: str, turn_id: str, status: str) -> None:
        await notify(account_id, session_id, turn_id, status)
        if status in TERMINAL:
            if store is not None:
                with contextlib.suppress(LucyError):
                    await store.apply_pending(account_id, session_id)
            await waker.flush(session_id)

    return ended


async def _bootstrap_session_workspace(
    client: EnvironmentsClient, workspace: WorkspaceScope
) -> None:
    """Seed the journal files and a git baseline. Missing git is not a failed session."""
    env_id = workspace.environment_id
    listing = await client.files(env_id, workspace.root)
    names = {entry.name for entry in listing.entries}
    if PROGRESS_FILE not in names:
        await client.write(env_id, posixpath.join(workspace.root, PROGRESS_FILE), PROGRESS_STARTER)
    if TASKS_FILE not in names:
        await client.write(env_id, posixpath.join(workspace.root, TASKS_FILE), TASKS_STARTER)
    try:
        await client.run(env_id, GIT_INIT, cwd=workspace.root)
        await client.run(env_id, GIT_BASELINE, cwd=workspace.root)
    except DownstreamError:
        return


def _blobs_root(settings: Settings) -> Path | None:
    """A configured volume, a sibling of the database, or a process-owned temp tree."""
    if settings.blobs_path.strip():
        return Path(settings.blobs_path)
    if settings.database_path == ":memory:":
        return None
    return Path(settings.database_path).expanduser().resolve().parent / "blobs"


def _sibling_service_tokens(settings: Settings) -> dict[str, str]:
    """Lucy's own credentials for siblings that speak the two-credential internal contract.

    Empty values are omitted: PackHttp then mints a Bearer, which those surfaces refuse.
    That fail-closed is cheaper than silently calling the person-facing routes.
    """
    token = settings.memory_api_token.strip()
    return {MEMORY_AUDIENCE: token} if token else {}


__all__ = ["Container", "PackRequest", "build_container"]
