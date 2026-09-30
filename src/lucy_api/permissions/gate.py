"""Whether a write may run without asking, in one place.

A permission is the unit a person can answer — "run commands in your workspace" — never
one operation. The gate is consulted before a plan executes, and an uncovered write is
allowed: only declared permissions are asked about, so adding a tool cannot silently
invent a prompt. Getting that wrong in the permissive direction for a *declared*
permission is a privilege escalation; getting it wrong for an undeclared one is a
questionnaire nobody asked for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lucy_api.packs.base import Catalogue, Permission
    from lucy_api.packs.context import PackContext

WRITE_EFFECTS = frozenset({"write"})
ACCOUNT_PROFILE = "*"

ONCE_PREFIX = "once:"
"""How a one-time answer is keyed among the grants: by the call it answered, not its permission.

A person answering an approval card for one call is answering about that call. Keyed by
permission, as it was, one "yes" to `cd calculator && node test.js` let every
`workspace.run` for the rest of the turn through: the model found node missing, wrote two
files nobody was asked about and ran `python test.py`, and the person saw none of it. And a
"no, call it `weekly.md` instead" refused the corrected write too, because it denied the
permission rather than the file.
"""


@dataclass(frozen=True, slots=True)
class Blocked:
    """One write in the plan that cannot run until a person answers, or at all."""

    permission: str
    operation: str
    message: str
    denied: bool = False
    arguments: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    step: str = ""
    """The blocked step's id in the plan, so an approval can find what the step reads from."""


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
class Floors:
    """The policy floors a mode cannot lower.

    They travel together because they are decided together -- by the person's settings and
    the session, once per turn -- and read together, by every step the gate inspects. Loose
    keyword arguments threaded through two functions is how one of them gets defaulted on
    one path and not the other.
    """

    memory_write_policy: str = "ask_first"
    confirm_outward: bool = True
    approval_policy: str = "destructive_always_asks"
    incognito: bool = False
    """An incognito session writes no note, so asking the person to approve one asks for
    nothing. Read in a sent turn: "remember that my favourite editor is helix" parked for
    approval on `notes.setFact`, was approved, and then met the refusal the handler holds.
    The gate knows the session is incognito, and says so instead of asking."""

    @classmethod
    def of(cls, context: PackContext) -> Floors:
        """The floors one turn runs under, read in one place.

        The auto-mode audit read three of them and defaulted the approval policy, so under
        `spend_and_destructive_ask` a spend the person had approved was written down as a
        bypass the gate never granted. Two readers of the same settings is one too many.
        """
        policy = context.policy
        return cls(
            memory_write_policy=policy.memory_write_policy,
            confirm_outward=policy.confirm_outward_actions,
            approval_policy=policy.approval_policy,
            incognito=context.incognito,
        )


DEFAULT_FLOORS = Floors()
"""What a plan is judged under when no turn is behind it: the settings' own defaults."""

INCOGNITO = "This session is incognito: notes are neither read nor written."
NOTES_PERMISSIONS = frozenset({"notes.write", "notes.erase"})


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
        floors: Floors = DEFAULT_FLOORS,
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
            raw_input = step.get("input")
            arguments = raw_input if isinstance(raw_input, dict) else {}
            verdict = _decide(
                permission,
                mode=mode,
                grants=grants,
                floors=floors,
                once=grants.get(once_key(name, arguments)),
            )
            if not verdict.allowed:
                item = Blocked(
                    permission=verdict.permission,
                    operation=name,
                    message=verdict.message,
                    denied=verdict.denied,
                    arguments=arguments,
                    description=str(step.get("note") or verdict.message),
                    step=str(step.get("id") or ""),
                )
                blocked.append(item)
                if first is None:
                    first = replace(verdict, operation=name)
            elif verdict.bypassed and permission.id not in auto_bypassed:
                auto_bypassed.append(permission.id)
        if first is not None:
            return replace(first, blocked=tuple(blocked), auto_bypassed=tuple(auto_bypassed))
        return Verdict(allowed=True, auto_bypassed=tuple(auto_bypassed))


def once_key(operation: str, arguments: Mapping[str, object]) -> str:
    """The grants key of a one-time answer to exactly this call.

    Arguments are canonicalised -- keys sorted, no whitespace -- so the same call planned again
    after the turn resumes matches whatever order the model wrote its fields in, and any
    change to what the call would do is a different call, asked about again.
    """
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    digest = sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{ONCE_PREFIX}{operation}:{digest}"


def _is_gated(permission: Permission, name: str, catalogue: Catalogue | None) -> bool:
    effects = _effects(name, catalogue)
    return effects in WRITE_EFFECTS or permission.risk in {"execute", "destructive", "spend"}


def _decide(
    permission: Permission,
    *,
    mode: str,
    grants: Mapping[str, Grant],
    floors: Floors,
    once: Grant | None = None,
) -> Verdict:
    """`once` is the person's answer to this exact call, when there is one. It stands in for
    the permission's standing grant and goes through the same floors in the same order, so a
    one-time yes can never lift a floor a standing yes could not."""
    floor = _denied_by_floor(permission, floors)
    if floor is not None:
        return floor
    grant = once or grants.get(permission.id) or grants.get(f"{ACCOUNT_PROFILE}:{permission.id}")
    if grant is not None and grant.decision.startswith("deny"):
        message = grant.instruction or f"{permission.title} is not allowed."
        return Verdict(False, message, permission.id, permission.title, denied=True)
    if grant is not None and grant.decision.startswith("allow"):
        return Verdict(True)
    if (
        permission.id == "notes.write"
        and floors.memory_write_policy == "automatic"
        and mode != "plan"
    ):
        return Verdict(True, bypassed=True)
    if floors.confirm_outward and permission.outward and mode != "plan":
        return Verdict(
            False,
            f"{permission.title} is something other people will see, so it needs approval.",
            permission.id,
            permission.title,
        )
    return _mode_verdict(permission, mode, floors.approval_policy)


def _denied_by_floor(permission: Permission, floors: Floors) -> Verdict | None:
    """The floors that deny a note write before any grant is read, so no answer lifts them."""
    if floors.incognito and permission.id in NOTES_PERMISSIONS:
        return Verdict(False, INCOGNITO, permission.id, permission.title, denied=True)
    if permission.id == "notes.write" and floors.memory_write_policy == "never":
        return Verdict(
            False,
            "Remembering is off. Only an explicit request from the person writes a new note.",
            permission.id,
            permission.title,
            denied=True,
        )
    return None


def _mode_verdict(permission: Permission, mode: str, approval_policy: str) -> Verdict:
    if mode == "plan":
        return Verdict(
            False,
            f"{permission.title} is a write; plan mode is read-only.",
            permission.id,
            permission.title,
            denied=True,
        )
    floor = _approval_floor(permission, approval_policy)
    if floor is not None:
        return floor
    if mode == "auto":
        return Verdict(True, bypassed=True)
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


def _approval_floor(permission: Permission, approval_policy: str) -> Verdict | None:
    """A stored grant may skip this. Auto mode may not: that is what makes it a floor."""
    if permission.risk == "destructive" and approval_policy in {
        "destructive_always_asks",
        "spend_and_destructive_ask",
    }:
        return Verdict(
            False,
            f"{permission.title} is destructive, so it needs approval.",
            permission.id,
            permission.title,
        )
    if permission.risk == "spend" and approval_policy == "spend_and_destructive_ask":
        return Verdict(
            False,
            f"{permission.title} spends money, so it needs approval.",
            permission.id,
            permission.title,
        )
    return None


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
    """``op``: the one key weftai's plan takes, and every plan is checked before the gate sees
    it. Reading ``operation``, ``tool`` and ``name`` as well, as this once did, let a step the
    executor would refuse be asked about, and then replayed under an empty operation."""
    value = step.get("op")
    return str(value) if value else ""


def _effects(name: str, catalogue: Catalogue | None) -> str:
    if catalogue is None:
        return "read"
    for operation in catalogue.operations():
        if operation.name == name:
            return str(operation.effects)
    return "read"


__all__ = [
    "ACCOUNT_PROFILE",
    "ONCE_PREFIX",
    "Blocked",
    "Grant",
    "PermissionGate",
    "Verdict",
    "once_key",
]
