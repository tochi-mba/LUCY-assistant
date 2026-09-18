"""Whether a write may run without asking, in one place.

A permission is the unit a person can answer — "run commands in your workspace" — never
one operation. The gate is consulted before a plan executes, and an uncovered write is
allowed: only declared permissions are asked about, so adding a tool cannot silently
invent a prompt. Getting that wrong in the permissive direction for a *declared*
permission is a privilege escalation; getting it wrong for an undeclared one is a
questionnaire nobody asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucy_api.packs.base import Catalogue, Permission

WRITE_EFFECTS = frozenset({"write"})
ACCOUNT_PROFILE = "*"


@dataclass(frozen=True, slots=True)
class Blocked:
    """One write in the plan that cannot run until a person answers, or at all."""

    permission: str
    operation: str
    message: str
    denied: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the gate decided, with the sentence the model or the person should read."""

    allowed: bool
    message: str = ""
    permission: str = ""
    title: str = ""
    operation: str = ""
    denied: bool = False
    """True when a grant or mode forbids the write. False when a person still has to answer."""

    bypassed: bool = False
    """True when auto or accept_edits let a write through without a stored grant."""

    blocked: tuple[Blocked, ...] = ()
    auto_bypassed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Grant:
    permission: str
    decision: str
    profile: str
    instruction: str = ""
    source: str = "person"


class PermissionGate:
    """Mode, grants, and the catalogue's permission list, applied to one plan."""

    def inspect(
        self,
        plan: Mapping[str, object],
        *,
        mode: str,
        grants: Mapping[str, Grant],
        catalogue: Catalogue | None,
        memory_write_policy: str = "ask_first",
    ) -> Verdict:
        permissions = _permissions(catalogue)
        by_operation = _covers(permissions)
        steps = _steps_of(plan)
        blocked: list[Blocked] = []
        auto_bypassed: list[str] = []
        first: Verdict | None = None
        for step in steps:
            if not isinstance(step, dict):
                continue
            name = _operation_name(step)
            if not name:
                continue
            permission = _permission_for(name, by_operation, permissions)
            if permission is None:
                continue
            if not _is_gated(permission, name, catalogue):
                continue
            verdict = _decide(
                permission,
                mode=mode,
                grants=grants,
                memory_write_policy=memory_write_policy,
            )
            raw_input = step.get("input")
            arguments = raw_input if isinstance(raw_input, dict) else {}
            if not verdict.allowed:
                item = Blocked(
                    permission=verdict.permission,
                    operation=name,
                    message=verdict.message,
                    denied=verdict.denied,
                    arguments=arguments,
                    description=str(step.get("note") or verdict.message),
                )
                blocked.append(item)
                if first is None:
                    first = replace(verdict, operation=name)
            elif verdict.bypassed and permission.id not in auto_bypassed:
                auto_bypassed.append(permission.id)
        if first is not None:
            return replace(first, blocked=tuple(blocked), auto_bypassed=tuple(auto_bypassed))
        return Verdict(allowed=True, auto_bypassed=tuple(auto_bypassed))


def _is_gated(permission: Permission, name: str, catalogue: Catalogue | None) -> bool:
    effects = _effects(name, catalogue)
    return effects in WRITE_EFFECTS or permission.risk in {"execute", "destructive", "spend"}


def _decide(
    permission: Permission,
    *,
    mode: str,
    grants: Mapping[str, Grant],
    memory_write_policy: str = "ask_first",
) -> Verdict:
    grant = grants.get(permission.id) or grants.get(f"{ACCOUNT_PROFILE}:{permission.id}")
    if grant is not None and grant.decision.startswith("deny"):
        message = grant.instruction or f"{permission.title} is not allowed."
        return Verdict(False, message, permission.id, permission.title, denied=True)
    if permission.id == "notes.write" and memory_write_policy == "never":
        return Verdict(
            False,
            "Remembering is off. Only an explicit request from the person writes a new note.",
            permission.id,
            permission.title,
            denied=True,
        )
    if grant is not None and grant.decision.startswith("allow"):
        return Verdict(True)
    if permission.id == "notes.write" and memory_write_policy == "automatic" and mode != "plan":
        return Verdict(True, bypassed=True)
    return _mode_verdict(permission, mode)


def _mode_verdict(permission: Permission, mode: str) -> Verdict:
    if mode == "auto":
        return Verdict(True, bypassed=True)
    if mode == "plan":
        return Verdict(
            False,
            f"{permission.title} is a write; plan mode is read-only.",
            permission.id,
            permission.title,
            denied=True,
        )
    if (
        mode == "accept_edits"
        and permission.risk == "write"
        and permission.id.startswith("workspace.")
    ):
        return Verdict(True, bypassed=True)
    return Verdict(
        False,
        f"{permission.title} needs approval before it can run.",
        permission.id,
        permission.title,
    )


def _permissions(catalogue: Catalogue | None) -> tuple[Permission, ...]:
    if catalogue is None:
        return ()
    found: list[Permission] = []
    for item in catalogue.bound:
        found.extend(item.pack.permissions())
    return tuple(found)


def _covers(permissions: Sequence[Permission]) -> dict[str, Permission]:
    mapping: dict[str, Permission] = {}
    for permission in permissions:
        for name in permission.covers:
            if name.endswith(".*"):
                continue
            mapping[name] = permission
    return mapping


def _permission_for(
    name: str, by_operation: dict[str, Permission], permissions: Sequence[Permission]
) -> Permission | None:
    found = by_operation.get(name)
    if found is not None:
        return found
    for permission in permissions:
        for pattern in permission.covers:
            if pattern.endswith(".*") and name.startswith(pattern[:-1]):
                return permission
    return None


def _steps_of(plan: Mapping[str, object]) -> tuple[object, ...]:
    steps = plan.get("steps")
    if isinstance(steps, list | tuple):
        return tuple(steps)
    return ()


def _operation_name(step: Mapping[str, object]) -> str:
    value = step.get("op") or step.get("operation") or step.get("tool") or step.get("name")
    return str(value) if value else ""


def _effects(name: str, catalogue: Catalogue | None) -> str:
    if catalogue is None:
        return "read"
    for operation in catalogue.operations():
        if operation.name == name:
            return str(operation.effects)
    return "read"


__all__ = ["ACCOUNT_PROFILE", "Blocked", "Grant", "PermissionGate", "Verdict"]
