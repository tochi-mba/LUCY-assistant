"""Turning a probed catalogue into the tools one turn, or one HTTP read, can see.

Routers never import weftai. They ask this module for dictionaries. The registry and the
runtime live here so a composition root can hand the supervisor an object that already
knows which packs this deployment installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.packs.context import PackContext, SilentTokens
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import NullHttp
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.registry import (
    build_registry,
    build_runtime,
    choose_bound,
    limits_for,
    plan_schema_for,
    probe_all,
)
from lucy_api.packs.work import WorkPack
from lucy_api.turn.window import without_needles

if TYPE_CHECKING:
    from collections.abc import Sequence

    from weftai.registry import Registry

    from lucy_api.packs.base import CapabilityPack, Catalogue
    from lucy_api.packs.context import Http, TokenSource
    from lucy_api.sessions.scope import SessionScope
    from lucy_api.work import Registry as WorkRegistry


def installed_packs(
    *, memory_base_url: str = "http://127.0.0.1:8009"
) -> tuple[CapabilityPack, ...]:
    """What this build ships. Third-party packs arrive through entry points later.

    Notes is always installed. When the notes service is down the probe marks it
    unavailable, which is how the model learns the difference between "this deployment has
    no notes" and "notes could not be reached this turn".

    Work is always installed too, and for the opposite reason: it has no downstream and
    nothing to be unavailable. It is the capability a model reaches for precisely when
    something else is slow, so it must not be the one that disappears when things are.
    """
    return (HelpPack(), NotesPack(memory_base_url), WorkPack())


class Capabilities:
    """Installed packs, plus the per-session set of capabilities the model asked to bind."""

    def __init__(
        self,
        packs: Sequence[CapabilityPack] | None = None,
        *,
        work: WorkRegistry | None = None,
    ) -> None:
        self.packs = tuple(packs) if packs is not None else installed_packs()
        self.work = work
        self._uses: dict[str, list[str]] = {}

    def remember_use(self, session_id: str, pack_id: str) -> None:
        used = self._uses.setdefault(session_id, [])
        if pack_id not in used:
            used.append(pack_id)

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
            permission_mode=scope.permission_mode,
            incognito=scope.incognito,
            bound_ids=set(self.recent(scope.session_id)),
            work=self.work,
        )

    async def probe(self, context: PackContext) -> Catalogue:
        catalogue = await probe_all(self.packs, context)
        context.catalogue = catalogue
        return catalogue

    def bound_for(self, catalogue: Catalogue, session_id: str) -> tuple[Any, tuple[str, ...]]:
        recent = (*self.recent(session_id), *sorted(context_ids(catalogue)))
        return choose_bound(catalogue, recent=recent)

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
        bound, deferred = choose_bound(catalogue, recent=self.recent(session_id))
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
        bound, _deferred = choose_bound(catalogue, recent=self.recent(session_id))
        operations = tuple(operation for item in bound for operation in item.operations)
        return build_registry(operations)

    def runtime_for(self, catalogue: Catalogue, session_id: str) -> Any:
        bound, _deferred = choose_bound(catalogue, recent=self.recent(session_id))
        return build_runtime(self.registry_for(catalogue, session_id), limits=limits_for(bound))

    def plan_schema(self, catalogue: Catalogue, session_id: str) -> dict[str, Any]:
        return plan_schema_for(self.registry_for(catalogue, session_id))

    async def execute(self, plan: dict[str, Any], context: PackContext) -> dict[str, Any]:
        """Run one plan against the tools this turn actually bound.

        Writes are allowed: ``capabilities.use`` is a write, and refusing it here would
        make deferred loading a one-way door. Read-only steps still run concurrently
        because weftai serialises only the ones that are not.
        """
        catalogue = context.catalogue
        if catalogue is None:
            catalogue = await self.probe(context)
        runtime = self.runtime_for(catalogue, context.session_id)
        result = await runtime.execute(
            without_needles(plan),
            {
                "ctx": context,
                "session": {"id": context.session_id},
                "allowWrites": True,
            },
        )
        for pack_id in context.bound_ids:
            self.remember_use(context.session_id, pack_id)
        return as_loop_result(result)


def context_ids(catalogue: Catalogue) -> tuple[str, ...]:
    return tuple(item.pack.id for item in catalogue.ready())


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
