"""Turning a probed catalogue into the tools one turn, or one HTTP read, can see.

Routers never import weftai. They ask this module for dictionaries. The registry and the
runtime live here so a composition root can hand the supervisor an object that already
knows which packs this deployment installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError, conflict
from lucy_api.packs.agents import AgentsPack
from lucy_api.packs.context import PackContext, SilentTokens
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import NullHttp
from lucy_api.packs.music import MusicPack
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.probes import ProbeCache, ProviderLocks
from lucy_api.packs.registry import (
    build_registry,
    build_runtime,
    choose_bound,
    limits_for,
    plan_schema_for,
    probe_all,
)
from lucy_api.packs.research import ResearchPack
from lucy_api.packs.settings import SettingsPack
from lucy_api.packs.work import WorkPack
from lucy_api.packs.workspace import WorkspacePack
from lucy_api.permissions.gate import PermissionGate
from lucy_api.turn.window import without_needles

NOT_FOUND = "not-found"
TOOL_FAILED = "this tool could not run"

if TYPE_CHECKING:
    from collections.abc import Sequence

    from weftai.registry import Registry

    from lucy_api.packs.base import CapabilityPack, Catalogue
    from lucy_api.packs.context import ChildRuntime, Http, TokenSource
    from lucy_api.sessions.scope import SessionScope
    from lucy_api.work import Registry as WorkRegistry


def installed_packs(  # noqa: PLR0913 -- one base URL per sibling this build ships
    *,
    memory_base_url: str = "http://127.0.0.1:8009",
    user_base_url: str = "http://127.0.0.1:8002",
    spotify_base_url: str = "http://127.0.0.1:8007",
    search_base_url: str = "http://127.0.0.1:8006",
    settings_base_url: str = "http://127.0.0.1:8003",
    environments_base_url: str = "http://127.0.0.1:8008",
) -> tuple[CapabilityPack, ...]:
    """What this build ships. Third-party packs arrive through entry points later.

    Notes is always installed. When the notes service is down the probe marks it
    unavailable, which is how the model learns the difference between "this deployment has
    no notes" and "notes could not be reached this turn".

    Work is always installed too, and for the opposite reason: it has no downstream and
    nothing to be unavailable. It is the capability a model reaches for precisely when
    something else is slow, so it must not be the one that disappears when things are.
    """
    return (
        HelpPack(),
        NotesPack(memory_base_url, user_base_url=user_base_url),
        ResearchPack(search_base_url),
        MusicPack(spotify_base_url),
        SettingsPack(settings_base_url),
        WorkspacePack(environments_base_url),
        WorkPack(),
        AgentsPack(),
    )


class Capabilities:
    """Installed packs, plus the per-session set of capabilities the model asked to bind."""

    def __init__(
        self,
        packs: Sequence[CapabilityPack] | None = None,
        *,
        work: WorkRegistry | None = None,
        probes: ProbeCache | None = None,
    ) -> None:
        self.packs = tuple(packs) if packs is not None else installed_packs()
        self.work = work
        self.child: ChildRuntime | None = None
        self.probes = probes if probes is not None else ProbeCache()
        self.providers = ProviderLocks()
        self._uses: dict[str, list[str]] = {}

    def forget_probes(self, account_id: str, profile: str, pack_id: str | None = None) -> None:
        """Drop cached availability so the next turn asks the pack again."""
        self.probes.drop(account_id, profile, pack_id)

    def remember_use(self, session_id: str, pack_id: str) -> None:
        """Put a capability at the front of this session's recency: most recently used first.

        It used to append and never move, so recency meant "first used first" and
        `KEEP_RECENT` kept whichever capabilities a session happened to touch first. Once
        four had been used, `capabilities.use` on a fifth answered `bound: true` and the
        capability was never bound -- the one-way door `ALWAYS` exists to prevent.
        """
        used = self._uses.setdefault(session_id, [])
        if pack_id in used:
            used.remove(pack_id)
        used.insert(0, pack_id)

    def recent(self, session_id: str) -> tuple[str, ...]:
        return tuple(self._uses.get(session_id, ()))

    def context_for(
        self,
        scope: SessionScope,
        *,
        http: Http | None = None,
        tokens: TokenSource | None = None,
    ) -> PackContext:
        """One turn's context for a capability, from the session it belongs to.

        The identity arrives as a `SessionScope` rather than as three loose strings,
        because a scope is derived once from a verified token and a stored row and cannot
        be assembled from the wrong pieces. Six parameters where three of them had to
        agree is six chances for a background job to run under the wrong profile.
        """
        return PackContext(
            account_id=scope.account_id,
            profile=scope.profile,
            session_id=scope.session_id,
            http=http if http is not None else NullHttp(),
            tokens=tokens if tokens is not None else SilentTokens(),
            turn_id=scope.turn_id,
            agent_id=scope.agent_id,
            depth=scope.depth,
            permission_mode=scope.permission_mode,
            incognito=scope.incognito,
            workspace_environment_id=(
                scope.workspace.environment_id if scope.workspace is not None else ""
            ),
            workspace_path=scope.workspace_root,
            work=self.work,
            child=self.child,
            probes=self.probes,
        )

    async def probe(self, context: PackContext) -> Catalogue:
        catalogue = await probe_all(self.packs, context)
        context.catalogue = catalogue
        return catalogue

    def bound_for(self, catalogue: Catalogue, session_id: str) -> tuple[Any, tuple[str, ...]]:
        """What this turn can call, and what it is holding back.

        The single answer. It used to be one of two: this method seeded recency with every
        ready pack while `tools`, `registry_for` and `runtime_for` each called `choose_bound`
        themselves, so the list the prompt could have shown and the list the schema was built
        from were computed by different code with nothing keeping them equal. Nothing called
        this one, which is the only reason they never visibly disagreed.
        """
        bound, deferred = choose_bound(catalogue, recent=self.recent(session_id))
        selected = {item.pack.id for item in bound} | set(catalogue.suggested)
        return (
            tuple(item for item in catalogue.ready() if item.pack.id in selected),
            tuple(name for name in deferred if name not in selected),
        )

    def listings(self, catalogue: Catalogue) -> list[dict[str, Any]]:
        return [
            {
                "id": item.pack.id,
                "title": item.pack.title,
                "summary": item.pack.summary,
                "state": item.availability.state.value,
                "detail": item.availability.detail,
                "offer_setup": item.availability.offer_setup,
                "usable": item.availability.usable,
            }
            for item in catalogue.bound
        ]

    def tools(self, catalogue: Catalogue, session_id: str) -> dict[str, Any]:
        bound, deferred = self.bound_for(catalogue, session_id)
        tools = [
            {
                "name": operation.name,
                "description": operation.description,
                "effects": operation.effects,
            }
            for item in bound
            for operation in item.operations
        ]
        return {"tools": tools, "deferred": list(deferred)}

    def registry_for(self, catalogue: Catalogue, session_id: str) -> Registry[Any]:
        bound, _deferred = self.bound_for(catalogue, session_id)
        operations = tuple(operation for item in bound for operation in item.operations)
        return build_registry(operations)

    def runtime_for(self, catalogue: Catalogue, session_id: str, context: PackContext) -> Any:
        bound, _deferred = self.bound_for(catalogue, session_id)
        return build_runtime(
            self.registry_for(catalogue, session_id),
            limits=limits_for(bound, context.policy),
            policy=context.policy,
        )

    def plan_schema(
        self, catalogue: Catalogue, session_id: str, context: PackContext
    ) -> dict[str, Any]:
        return plan_schema_for(self.registry_for(catalogue, session_id), context.policy)

    async def execute(self, plan: dict[str, Any], context: PackContext) -> dict[str, Any]:
        """Run one plan against the tools this turn actually bound.

        Writes are allowed: ``capabilities.use`` is a write, and refusing it here would
        make deferred loading a one-way door. Read-only steps still run concurrently
        because weftai serialises only the ones that are not.
        """
        catalogue = context.catalogue
        if catalogue is None:
            catalogue = await self.probe(context)
        verdict = PermissionGate().inspect(
            plan,
            mode=context.permission_mode,
            grants=context.grants,
            catalogue=catalogue,
            memory_write_policy=context.policy.memory_write_policy,
            confirm_outward=context.policy.confirm_outward_actions,
            approval_policy=context.policy.approval_policy,
        )
        if not verdict.allowed:
            return {
                "issues": [
                    {
                        "code": "permission_denied" if item.denied else "permission_required",
                        "message": item.message,
                        "permission": item.permission,
                        "operation": item.operation,
                        "arguments": item.arguments,
                        "description": item.description,
                    }
                    for item in verdict.blocked
                ],
                "text": verdict.message,
                "steps": [],
            }
        runtime = self.runtime_for(catalogue, context.session_id, context)
        result = await runtime.execute(
            without_needles(plan),
            {
                "ctx": context,
                "session": {"id": context.session_id},
                "allowWrites": True,
            },
        )
        # What ran is recent; what was explicitly asked for is more recent still, because
        # asking is the model saying it needs that capability next. Marking every bound
        # capability on every plan, as this once did, made recency mean nothing.
        for pack_id in _packs_run(plan, catalogue):
            self.remember_use(context.session_id, pack_id)
        for pack_id in context.bound_ids:
            self.remember_use(context.session_id, pack_id)
        context.bound_ids.clear()
        return as_loop_result(result)

    async def invoke(
        self, name: str, arguments: dict[str, Any], context: PackContext
    ) -> dict[str, Any]:
        """Run one named operation as a one-step plan. Same gate, no model tokens."""
        catalogue = context.catalogue
        if catalogue is None:
            catalogue = await self.probe(context)
        listing = self.tools(catalogue, context.session_id)
        bound = {tool["name"] for tool in listing["tools"]}
        if name not in bound:
            raise LucyError(NOT_FOUND, _unknown_tool(name, bound, listing["deferred"]), 404)
        result = await self.execute(
            {"steps": [{"id": "invoke", "op": name, "input": arguments}]},
            context,
        )
        issues = result.get("issues") or []
        if issues:
            first = issues[0] if isinstance(issues[0], dict) else {}
            message = str(first.get("message") or TOOL_FAILED)
            raise conflict(message)
        return result


def _unknown_tool(name: str, bound: set[str], deferred: list[str]) -> str:
    available = ", ".join(sorted(bound)) if bound else "no bound tools"
    message = f"Unknown tool `{name}`; this turn has {available}"
    if deferred:
        held = ", ".join(sorted(deferred))
        return f"{message}. Deferred: {held}. Bind one with capabilities.use."
    return f"{message}."


def _packs_run(plan: dict[str, Any], catalogue: Catalogue) -> tuple[str, ...]:
    """The capabilities a plan's steps belong to, in the order the plan first named them.

    Read from the catalogue rather than from the operation's prefix, because the prefix is
    not always the pack: `capabilities.use` belongs to `help`.
    """
    owner = {
        operation.name: item.pack.id for item in catalogue.bound for operation in item.operations
    }
    steps = plan.get("steps") if isinstance(plan, dict) else None
    found: list[str] = []
    for step in steps if isinstance(steps, list) else ():
        name = step.get("op") if isinstance(step, dict) else None
        pack_id = owner.get(name) if isinstance(name, str) else None
        if pack_id is not None and pack_id not in found:
            found.append(pack_id)
    return tuple(found)


def as_loop_result(result: Any) -> dict[str, Any]:
    """weftai's execution result, in the shape :func:`lucy_api.turn.loop.run_turn` executes."""
    issues = result.get("issues") if isinstance(result, dict) else None
    steps = result.get("steps") if isinstance(result, dict) else ()
    return {
        "issues": issues,
        "text": result.get("text") if isinstance(result, dict) else "",
        "steps": list(steps or ()),
    }


__all__ = ["Capabilities", "as_loop_result", "installed_packs"]
