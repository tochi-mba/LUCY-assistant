"""Long-running work as MCP tasks, only when this request declared the extension.

There is no protocol session to remember a handshake, so the gate is this request's
``_meta`` client capabilities. A client that did not opt in gets the same unknown-method
404 as any other missing method. Lucy never returns a task handle on the default
synchronous path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.mcp.handlers import McpCall, SessionGoneError, owned_session
from lucy_api.mcp.protocol import invalid_request, unknown_method
from lucy_api.work import UnknownWorkError

if TYPE_CHECKING:
    from lucy_api.work.types import Record

TASKS_CAP = "io.modelcontextprotocol/tasks"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
NEED_SESSION = "This task call needs a session_id from lucy_session_create."
NEED_TASK = "This task call needs a taskId from tasks/list."


def client_offers_tasks(params: dict[str, Any]) -> bool:
    """True only when *this* request named the tasks extension."""
    caps: object = None
    meta = params.get("_meta")
    if isinstance(meta, dict):
        caps = meta.get(META_CLIENT_CAPS)
    if not isinstance(caps, dict):
        caps = params.get("capabilities")
    if not isinstance(caps, dict):
        return False
    return TASKS_CAP in caps or "tasks" in caps


def _task(record: Record) -> dict[str, Any]:
    status = "working" if not record.state.finished else record.state.value
    return {
        "taskId": record.id,
        "status": status,
        "kind": record.kind.value,
        "role": record.role,
        "objective": record.objective,
        "progress": record.progress or record.detail,
    }


def _task_id(arguments: dict[str, Any]) -> str:
    raw = arguments.get("taskId") or arguments.get("task_id") or arguments.get("id")
    if not isinstance(raw, str) or not raw:
        raise invalid_request(NEED_TASK)
    return raw


async def handle(method: str, arguments: dict[str, Any], call: McpCall) -> dict[str, Any]:
    if not client_offers_tasks(arguments):
        raise unknown_method()
    if method == "tasks/list":
        return await _list(call, arguments)
    if method == "tasks/get":
        return await _get(call, arguments)
    if method == "tasks/cancel":
        return await _cancel(call, arguments)
    raise unknown_method()


async def _owned_running(
    call: McpCall, arguments: dict[str, Any]
) -> tuple[str, tuple[Record, ...]]:
    session_id = str(arguments.get("session_id") or "")
    if not session_id:
        raise invalid_request(NEED_SESSION)
    await owned_session(call, session_id)
    return session_id, call.container.work.running(session_id)


async def _list(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    _session_id, rows = await _owned_running(call, arguments)
    return {"tasks": [_task(record) for record in rows]}


async def _get(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    _session_id, rows = await _owned_running(call, arguments)
    work_id = _task_id(arguments)
    for record in rows:
        if record.id == work_id:
            return {"task": _task(record)}
    raise SessionGoneError


async def _cancel(call: McpCall, arguments: dict[str, Any]) -> dict[str, Any]:
    _session_id, rows = await _owned_running(call, arguments)
    work_id = _task_id(arguments)
    if work_id not in {record.id for record in rows}:
        raise SessionGoneError
    try:
        record = call.container.work.cancel(work_id)
    except UnknownWorkError as exc:
        raise SessionGoneError from exc
    return {"task": _task(record)}
