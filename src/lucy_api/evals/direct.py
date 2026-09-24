"""Operations the harness runs itself, in a scenario's session, without a model.

``seed`` plants something before the first turn; ``verify`` reads the world back after
one. Both go through ``POST /v1/tools/{name}/invoke`` scoped to the session, which applies
the same deferral and the same permission gate a turn does -- so two things have to be
arranged first, and both are arranged as narrowly as the hub allows:

**Binding.** A capability can be deferred on a fresh session. If the operation is not
callable yet and its capability is in the deferred list, the harness binds it the way the
model would, with ``capabilities.use``. That counts as a use of the capability in this
session, which a later turn will see as recent.

**Permission.** A write in an ``ask`` session answers 409 until somebody approves it. The
harness grants the one permission that covers the operation, for *this session only*
(the hub's ``session:{id}`` grant profile), runs it, and revokes the grant immediately --
so the person's own profile and account grants are never read, written or widened, and
the turns that follow still have to ask.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from lucy_api.evals.hub import SESSION_GRANT_PREFIX, HubError
from lucy_api.evals.results import InvocationRecord

if TYPE_CHECKING:
    from lucy_api.evals.hub import Hub
    from lucy_api.evals.scenario import Invocation

BIND = "capabilities.use"
UNAVAILABLE = "unavailable"
REFUSED = "refused"


def invoke(hub: Hub, invocation: Invocation, *, session_id: str, profile: str) -> InvocationRecord:
    """Run one operation and record how it ended. Only a fatal hub error escapes."""
    missing = _bind(hub, invocation.op, session_id)
    if missing:
        return _record(invocation, UNAVAILABLE, error=missing)
    try:
        body = _call(hub, invocation, session_id=session_id, profile=profile)
    except HubError as exc:
        if exc.fatal:
            raise
        return _record(invocation, REFUSED, error=f"{exc} (HTTP {exc.status})")
    return _outcome(invocation, body)


def _bind(hub: Hub, operation: str, session_id: str) -> str:
    """Nothing when ``operation`` is callable in this session; otherwise why it is not."""
    listing = hub.tools(session_id)
    if operation in _names(listing):
        return ""
    capability = operation.split(".", 1)[0]
    if capability in _deferred(listing):
        hub.invoke(BIND, {"id": capability}, session_id)
        listing = hub.tools(session_id)
        if operation in _names(listing):
            return ""
    callable_now = ", ".join(sorted(_names(listing))) or "nothing"
    held = ", ".join(sorted(_deferred(listing)))
    held_back = f"; deferred: {held}" if held else ""
    return f"this session cannot call {operation}; it can call {callable_now}{held_back}"


def _call(hub: Hub, invocation: Invocation, *, session_id: str, profile: str) -> dict[str, Any]:
    """Invoke once; on a 409, grant the covering permission for this session only, and retry."""
    try:
        return hub.invoke(invocation.op, invocation.input, session_id)
    except HubError as exc:
        conflict = exc.status == HTTPStatus.CONFLICT
        permission = _covering(hub.permissions(profile), invocation.op) if conflict else ""
        if not permission:
            raise
        return _granted(hub, invocation, session_id=session_id, permission=permission)


def _granted(
    hub: Hub, invocation: Invocation, *, session_id: str, permission: str
) -> dict[str, Any]:
    """Run it under a grant that lasts exactly as long as this one call."""
    scope = f"{SESSION_GRANT_PREFIX}{session_id}"
    hub.grant(permission, scope)
    try:
        return hub.invoke(invocation.op, invocation.input, session_id)
    finally:
        hub.revoke(permission, scope)


def _covering(permissions: list[dict[str, Any]], operation: str) -> str:
    """The permission id that covers ``operation``, as the hub's catalogue declares it."""
    for row in permissions:
        covers = row.get("covers")
        if isinstance(covers, list) and any(
            fnmatchcase(operation, str(pattern)) for pattern in covers
        ):
            return str(row.get("id") or row.get("permission") or "")
    return ""


def _outcome(invocation: Invocation, body: dict[str, Any]) -> InvocationRecord:
    steps = body.get("steps")
    step = steps[0] if isinstance(steps, list) and steps and isinstance(steps[0], dict) else None
    if step is None:
        return _record(invocation, "error", error="the hub answered without running a step")
    return _record(
        invocation,
        str(step.get("status") or "error"),
        output=_render(step.get("data")),
        error=str(step.get("error") or step.get("skippedBecause") or ""),
    )


def _record(
    invocation: Invocation, status: str, *, output: str = "", error: str = ""
) -> InvocationRecord:
    return InvocationRecord(
        op=invocation.op, input=invocation.input, status=status, output=output, error=error
    )


def _render(data: object) -> str:
    """Output as text a regex can search: strings as they are, anything else as JSON."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _names(listing: dict[str, Any]) -> set[str]:
    tools = listing.get("tools")
    rows = tools if isinstance(tools, list) else []
    return {str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")}


def _deferred(listing: dict[str, Any]) -> set[str]:
    deferred = listing.get("deferred")
    return {str(item) for item in deferred} if isinstance(deferred, list) else set()


__all__ = ["BIND", "REFUSED", "UNAVAILABLE", "invoke"]
