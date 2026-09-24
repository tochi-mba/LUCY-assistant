"""Session-confined files and commands in the attached workspace."""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Any, Literal

from weftai.operation import define_operation
from weftai.schema.spec import boolean_schema, integer_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.environments import (
    AUDIENCE,
    DEFAULT_OUTPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    HttpEnvironmentsClient,
)
from lucy_api.clients.errors import DownstreamError
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.collections import FILE
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc
from lucy_api.sessions.scope import ConfinementError
from lucy_api.work import AtCapacityError, StillRunningError
from lucy_api.work.types import Brief, Kind
from lucy_api.workspace.text import (
    BINARY_NOTICE,
    DEFAULT_LINE_LIMIT,
    apply_edit,
    digest,
    is_binary,
    numbered_window,
    stale_if_changed,
    validate_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.environments import EnvironmentsClient, Mutation
    from lucy_api.packs.context import PackContext


MAX_TOOL_OUTPUT_CHARS = 8_000
ABSOLUTE_PATH = "workspace paths must be relative to this session"
OUTSIDE_SESSION = "workspace path resolves outside this session"


class WorkspacePack:
    id = "workspace"
    title = "Workspace"
    summary = "Read, search, edit and run commands inside this conversation's sandbox."

    def __init__(
        self,
        base_url: str,
        *,
        audience: str = AUDIENCE,
        client: EnvironmentsClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="workspace.change",
                title="Change workspace files",
                description="Write, edit, patch or move files in this session.",
                risk="write",
                covers=(
                    "workspace.write",
                    "workspace.edit",
                    "workspace.patch",
                    "workspace.move",
                ),
            ),
            Permission(
                id="workspace.destroy",
                title="Delete workspace files",
                description=(
                    "Delete a file in this session. Auto mode still asks, unless you "
                    "already allowed this."
                ),
                risk="destructive",
                covers=("workspace.delete",),
            ),
            Permission(
                id="workspace.run",
                title="Run workspace commands",
                description="Run a command inside this session's sandbox subtree.",
                risk="execute",
                covers=("workspace.run",),
            ),
        )

    def setup(self) -> SetupPlan | None:
        # Session creation provisions this capability. There is deliberately no manual
        # attach flow that could select another conversation's environment.
        return None

    async def probe(self, context: PackContext) -> Availability:
        if not context.workspace_environment_id or not context.workspace_path:
            return Availability(state=State.not_connected, detail="no workspace is attached")
        try:
            ready = await self._client(context).ready()
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(state=State.unavailable, detail="workspace could not be reached")
        if not ready.ready:
            return Availability(state=State.unavailable, detail="workspace is not ready")
        return Availability(state=State.ready, detail=f"attached ({ready.sandbox_tier})")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        del context
        return (
            self._operation(
                "list",
                "List files and directories in this session.",
                {"path": string_schema().optional()},
                FILE,
                self._list,
            ),
            self._operation(
                "grep",
                "Search workspace files for literal text.",
                {"pattern": string_schema(), "path": string_schema().optional()},
                value(object_schema({})),
                self._grep,
            ),
            self._operation(
                "read",
                "Read a bounded UTF-8 file window.",
                {
                    "path": string_schema(),
                    "offset": integer_schema().optional(),
                    "max_bytes": integer_schema().optional(),
                    "start_line": integer_schema().optional(),
                    "limit": integer_schema().optional(),
                },
                value(object_schema({})),
                self._read,
            ),
            self._operation(
                "write",
                "Write or append a UTF-8 workspace file.",
                {
                    "path": string_schema(),
                    "content": string_schema(),
                    "mode": string_schema().optional(),
                    "if_match": string_schema().optional(),
                },
                value(object_schema({})),
                self._write,
                effects="write",
            ),
            self._operation(
                "edit",
                "Replace one exact occurrence; ambiguity is refused.",
                {
                    "path": string_schema(),
                    "old_string": string_schema(),
                    "new_string": string_schema(),
                    "if_match": string_schema().optional(),
                },
                value(object_schema({})),
                self._edit,
                effects="write",
            ),
            self._operation(
                "patch",
                "Apply a single-file unified patch and report rejected hunks.",
                {
                    "path": string_schema(),
                    "patch": string_schema(),
                    "if_match": string_schema().optional(),
                },
                value(object_schema({})),
                self._patch,
                effects="write",
            ),
            self._operation(
                "delete",
                "Delete one workspace path; recursive must be explicit.",
                {"path": string_schema(), "recursive": boolean_schema().optional()},
                value(object_schema({})),
                self._delete,
                effects="write",
            ),
            self._operation(
                "move",
                "Move one regular file without overwriting its destination.",
                {"source": string_schema(), "destination": string_schema()},
                value(object_schema({})),
                self._move,
                effects="write",
            ),
            self._operation(
                "run",
                "Run a command inside this session's workspace subtree. "
                "Long commands return a handle; check work.check or work.wait. "
                "With wake, a command that finishes while nobody is talking wakes the session.",
                {
                    "command": string_schema(),
                    "timeout_ms": integer_schema().optional(),
                    "wait": boolean_schema().optional(),
                    "wait_seconds": integer_schema().optional(),
                    "wake": boolean_schema().optional(),
                },
                value(object_schema({})),
                self._run,
                effects="write",
            ),
        )

    def _operation(  # noqa: PLR0913 - operation definitions have six orthogonal fields
        self,
        name: str,
        description: str,
        inputs: dict[str, Any],
        output: Any,
        handler: Any,
        *,
        effects: Literal["read", "write"] = "read",
    ) -> AnyOperation:
        return define_operation(
            {
                "name": f"workspace.{name}",
                "description": description,
                "input": object_schema(inputs),
                "output": output,
                "effects": effects,
                "run": handler,
            }
        )

    def _client(self, context: PackContext) -> EnvironmentsClient:
        return self._override or HttpEnvironmentsClient(
            context.http, self.base_url, audience=self.audience
        )

    async def _list(self, run: RunContext[PackContext]) -> list[dict[str, Any]]:
        listing = await self._client(run.ctx).files(
            run.ctx.workspace_environment_id,
            confined_path(run.ctx, str(run.input.get("path") or ".")),
        )
        return [
            {
                "name": item.name,
                "path": _relative(run.ctx, item.path),
                "kind": item.kind,
                "bytes": item.size,
            }
            for item in listing.entries
        ]

    async def _grep(self, run: RunContext[PackContext]) -> dict[str, Any]:
        result = await self._client(run.ctx).search(
            run.ctx.workspace_environment_id,
            confined_path(run.ctx, str(run.input.get("path") or ".")),
            str(run.input.get("pattern") or ""),
        )
        return {
            "matches": [
                {"path": _relative(run.ctx, match.path), "lines": list(match.lines)}
                for match in result.matches
            ],
            "total_matches": result.total_matches,
            "truncated": result.truncated,
            "skipped": {
                "binary": list(result.skipped_binary),
                "large": list(result.skipped_large),
                "unavailable": list(result.skipped_unavailable),
            },
        }

    async def _read(self, run: RunContext[PackContext]) -> dict[str, Any]:
        content = await self._client(run.ctx).read(
            run.ctx.workspace_environment_id,
            confined_path(run.ctx, str(run.input.get("path") or "")),
            offset=int(run.input.get("offset") or 0),
            max_bytes=_optional_int(run.input.get("max_bytes")),
        )
        relative = _relative(run.ctx, content.path)
        if is_binary(content.content):
            return {
                "path": relative,
                "binary": True,
                "size": content.size,
                "notice": BINARY_NOTICE,
            }
        window = numbered_window(
            content.content,
            start_line=max(1, int(run.input.get("start_line") or 1)),
            limit=max(1, int(run.input.get("limit") or DEFAULT_LINE_LIMIT)),
            truncated=content.truncated,
        )
        notice = window.notice
        if content.notice:
            notice = f"{window.notice}; {content.notice}"
        return {
            "path": relative,
            "content": window.numbered,
            "size": content.size,
            "offset": content.offset,
            "start_line": window.start_line,
            "end_line": window.end_line,
            "total_lines": window.total_lines,
            "truncated": content.truncated,
            "file_fingerprint": window.file_digest,
            "window_fingerprint": window.window_digest,
            "notice": notice,
        }

    async def _write(self, run: RunContext[PackContext]) -> dict[str, Any]:
        env_id = run.ctx.workspace_environment_id
        path = confined_path(run.ctx, str(run.input.get("path") or ""))
        relative = _relative(run.ctx, path)
        refused = await _refuse_stale(
            self._client(run.ctx), env_id, path, str(run.input.get("if_match") or "")
        )
        if refused is not None:
            return {**refused, "path": relative}
        body = str(run.input.get("content") or "")
        problem = validate_text(path, body)
        if problem:
            return {"path": _relative(run.ctx, path), "written": False, "notice": problem}
        result = await self._client(run.ctx).write(
            env_id, path, body, mode=str(run.input.get("mode") or "overwrite")
        )
        return {
            "path": _relative(run.ctx, result.path),
            "size": result.size,
            "written": True,
            "file_fingerprint": digest(body),
        }

    async def _edit(self, run: RunContext[PackContext]) -> dict[str, Any]:
        env_id = run.ctx.workspace_environment_id
        path = confined_path(run.ctx, str(run.input.get("path") or ""))
        client = self._client(run.ctx)
        current = await client.read(env_id, path)
        relative = _relative(run.ctx, path)
        if is_binary(current.content):
            return {"path": relative, "replaced": False, "notice": BINARY_NOTICE}
        expected = str(run.input.get("if_match") or "")
        stale = stale_if_changed(current.content, expected)
        if stale:
            return {
                "path": relative,
                "replaced": False,
                "notice": stale,
                "file_fingerprint": digest(current.content),
            }
        applied = apply_edit(
            current.content,
            str(run.input.get("old_string") or ""),
            str(run.input.get("new_string") or ""),
        )
        if not applied.replaced or applied.match is None:
            return {
                "path": relative,
                "replaced": False,
                "notice": applied.notice,
                "file_fingerprint": digest(current.content),
            }
        problem = validate_text(path, applied.text)
        if problem:
            return {
                "path": relative,
                "replaced": False,
                "notice": problem,
                "file_fingerprint": digest(current.content),
            }
        new = str(run.input.get("new_string") or "")
        result = await client.edit(env_id, path, applied.match.text, new)
        return {
            **_mutation(run.ctx, result),
            "replaced": True,
            "rung": applied.match.rung,
            "file_fingerprint": digest(applied.text),
        }

    async def _patch(self, run: RunContext[PackContext]) -> dict[str, Any]:
        env_id = run.ctx.workspace_environment_id
        path = confined_path(run.ctx, str(run.input.get("path") or ""))
        relative = _relative(run.ctx, path)
        refused = await _refuse_stale(
            self._client(run.ctx), env_id, path, str(run.input.get("if_match") or "")
        )
        if refused is not None:
            return {**refused, "path": relative}
        result = await self._client(run.ctx).patch(
            env_id,
            path,
            str(run.input.get("patch") or ""),
        )
        return _mutation(run.ctx, result)

    async def _delete(self, run: RunContext[PackContext]) -> dict[str, Any]:
        path = str(run.input.get("path") or "")
        await self._client(run.ctx).delete(
            run.ctx.workspace_environment_id,
            confined_path(run.ctx, path),
            recursive=bool(run.input.get("recursive", False)),
        )
        return {"path": path, "deleted": True}

    async def _move(self, run: RunContext[PackContext]) -> dict[str, Any]:
        result = await self._client(run.ctx).move(
            run.ctx.workspace_environment_id,
            confined_path(run.ctx, str(run.input.get("source") or "")),
            confined_path(run.ctx, str(run.input.get("destination") or "")),
        )
        return _mutation(run.ctx, result)

    async def _run(self, run: RunContext[PackContext]) -> dict[str, Any]:
        command = str(run.input.get("command") or "")
        timeout_ms = max(1, min(int(run.input.get("timeout_ms") or DEFAULT_TIMEOUT_MS), 600_000))
        wait = run.input.get("wait", True)
        wait_flag = wait if isinstance(wait, bool) else True
        raw_wait = run.input.get("wait_seconds")
        wait_seconds = timeout_ms / 1000 if raw_wait is None else max(0.0, float(raw_wait))

        async def work() -> dict[str, Any]:
            result = await self._client(run.ctx).run(
                run.ctx.workspace_environment_id,
                command,
                cwd=run.ctx.workspace_path,
                timeout_ms=timeout_ms,
                max_output_bytes=DEFAULT_OUTPUT_BYTES,
            )
            output = result.output[:MAX_TOOL_OUTPUT_CHARS]
            later = max(0, len(result.output) - len(output)) + result.output_truncated_bytes
            omitted = later + result.output_dropped_bytes
            return {
                "command": result.command,
                "exit_code": result.exit_code,
                "output": output,
                "state": result.state,
                "timed_out": result.timed_out,
                "output_dropped_bytes": omitted,
                "notice": _output_notice(omitted, later),
            }

        registry = run.ctx.work
        if registry is None:
            return await work()
        try:
            handle = registry.start(
                work(),
                Brief(
                    session_id=run.ctx.session_id,
                    kind=Kind.command,
                    role="command",
                    objective=command[:160] or "run a workspace command",
                    timeout_seconds=timeout_ms / 1000,
                    account_id=run.ctx.account_id,
                    wake=bool(run.input.get("wake", False)),
                ),
            )
        except AtCapacityError as exc:
            return {"status": "busy", "message": str(exc)}
        if not wait_flag:
            return {
                "status": "running",
                "work_id": handle.id,
                "notice": "command started; check work.check or work.wait when you need the output",
            }
        try:
            finished = await registry.wait(handle.id, wait_seconds)
        except StillRunningError:
            return {
                "status": "running",
                "work_id": handle.id,
                "notice": (
                    f"command is still running after {wait_seconds:.0f}s; "
                    "it has not been stopped. Use work.check or work.wait."
                ),
            }
        payload = finished.payload
        return _completed_command(payload, handle.id)


def _output_notice(omitted: int, later: int) -> str:
    """What was left out of a command's output, and which end it was left out of.

    The output is always the *head*: the sandbox stops reading at its byte cap and this pack
    stops at its character cap, both from the start. That has to be said, because a test run
    or a build puts its verdict last, and a model told only "57000 characters omitted" reads
    the head as the whole story -- when the part it never saw was a megabyte ending in the
    failure it was asked about.
    """
    if not omitted:
        return ""
    notice = f"{omitted} output characters or bytes omitted"
    if later:
        notice += f"; this is the beginning of the output, and {later} of those came after it"
    return notice


def _completed_command(payload: object, work_id: str) -> dict[str, Any]:
    if isinstance(payload, dict):
        return {**payload, "work_id": work_id}
    return {"work_id": work_id, "result": payload}


def confined_path(context: PackContext, relative: str) -> str:
    """The absolute workspace path for a relative one, refused if it would leave the session.

    Shared with every capability that names a workspace file -- a watch on a build log uses
    it -- so "outside this session" has one definition and one test."""
    folded = relative.replace("\\", "/")
    if folded.startswith("/"):
        raise ConfinementError(ABSOLUTE_PATH)
    candidate = posixpath.normpath(posixpath.join(context.workspace_path, folded))
    if candidate != context.workspace_path and not candidate.startswith(
        context.workspace_path + "/"
    ):
        raise ConfinementError(OUTSIDE_SESSION)
    return candidate


def _relative(context: PackContext, path: str) -> str:
    prefix = context.workspace_path.rstrip("/") + "/"
    return path.removeprefix(prefix) if path != context.workspace_path else "."


def _mutation(context: PackContext, result: Mutation) -> dict[str, Any]:
    return {
        "path": _relative(context, result.path),
        "size": result.size,
        "diff": result.diff,
        "applied_hunks": list(result.applied_hunks),
        "rejected_hunks": list(result.rejected_hunks),
    }


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _refuse_stale(
    client: EnvironmentsClient, env_id: str, path: str, expected: str
) -> dict[str, Any] | None:
    if not expected:
        return None
    current = await client.read(env_id, path)
    stale = stale_if_changed(current.content, expected)
    if not stale:
        return None
    return {
        "written": False,
        "replaced": False,
        "notice": stale,
        "file_fingerprint": digest(current.content),
    }


__all__ = ["WorkspacePack"]
