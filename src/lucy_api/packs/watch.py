"""Saying when something happens, without the model looking for it.

"Tell me when CI is green." "When the export lands, summarise it." "Wait for that helper,
then carry on." Each of those is a condition somewhere outside the conversation, and the
wrong way to wait for one is the obvious way: a model that calls `workspace.run` every
thirty seconds spends the turn's budget on nothing and holds the conversation open for as
long as the wait lasts. So a watch is **work**: it gets a handle, it sits in the live block
with its last check, it ends with a notice, and it can wake the session when it fires.

Four things can be watched, and the choice is made by which field is given:

| field | fires when |
| --- | --- |
| `path` | the workspace file exists, or its contents match `pattern` |
| `url` | a public HTTPS address answers `expect_status` (200), or its body matches `pattern` |
| `work_id` | that piece of work finishes, however it ends |
| `command` (`watch.command`) | the command exits 0, or its output matches `pattern` |

`watch.command` is its own operation because it runs something, repeatedly, and that is a
thing a person is asked about once -- with the interval and the lifetime in the question --
rather than a thing that hides inside a read-only operation.

The result is *that* it fired, with a bounded excerpt of the evidence, never the file or
the page. A watch that never fires expires with one notice and the offer to start again.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

from weftai.operation import define_operation
from weftai.schema.spec import (
    boolean_schema,
    integer_schema,
    number_schema,
    object_schema,
    string_schema,
)
from weftai.schema.types import value

from lucy_api.clients.environments import (
    AUDIENCE,
    DEFAULT_OUTPUT_BYTES,
    HttpEnvironmentsClient,
    read_from,
)
from lucy_api.clients.errors import AbsentError
from lucy_api.core.errors import LucyError
from lucy_api.net.ssrf import REFUSED as ADDRESS_REFUSED
from lucy_api.net.ssrf import assert_public_https
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.workspace import confined_path
from lucy_api.prompt.docs import capability_doc
from lucy_api.sessions.scope import ConfinementError
from lucy_api.work import AtCapacityError, Brief, Check, Kind, UnknownWorkError, new_id, watch
from lucy_api.work.watch import (
    DEFAULT_EVERY_SECONDS,
    DEFAULT_FOR_SECONDS,
    MAX_EVERY_SECONDS,
    MAX_FOR_SECONDS,
    MIN_EVERY_SECONDS,
    clamp_every,
    clamp_for,
    compile_pattern,
    excerpt_around,
)

if TYPE_CHECKING:
    import re
    from collections.abc import Sequence
    from pathlib import Path

    import httpx
    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.environments import EnvironmentsClient
    from lucy_api.packs.context import PackContext
    from lucy_api.work import Registry
    from lucy_api.work.watch import Probe, Sleep

MAX_BODY = 200_000
"""How much of a file or a page one check reads. Enough for a log's verdict, not the log."""


BINARY_FILE = "binary file; a pattern only matches text"
"""What a file watch with a pattern says while it waits on a binary file.

The sandbox sends a binary file base64, and a pattern searched for in that matches the
encoding rather than the file: `watch.file` on a build artefact grepped base64 and could
fire on a run of letters that was never in it.
"""

COMMAND_TIMEOUT_MS = 60_000
"""How long one check's command may run. A check that takes longer than the interval is a
job, and a job is started with `workspace.run`, not watched."""

DEFAULT_STATUS = 200


class Fetch(Protocol):
    """One GET of a public HTTPS address: the status and a bounded body. Never a redirect."""

    async def __call__(self, url: str) -> tuple[int, str]: ...


def httpx_fetch(client: httpx.AsyncClient) -> Fetch:
    """The real fetch, on the hub's outbound client. Redirects are refused, not followed:
    a Location header is another address that never went through the egress check."""

    async def fetch(url: str) -> tuple[int, str]:
        response = await client.get(url, follow_redirects=False)
        return response.status_code, response.text[:MAX_BODY]

    return fetch


NO_WORKSPACE = "no workspace is attached to this session"


def _work_probe(registry: Registry, target: str) -> Probe | dict[str, Any]:
    try:
        registry.state_of(target)
    except UnknownWorkError as exc:
        return _refusal("unknown", str(exc))

    async def work_probe() -> Check:
        return _work_check(registry, target)

    return work_probe


class WatchPack:
    """The capability that turns "tell me when" into a promise Lucy can keep."""

    id = "watch"
    title = "Watch for something"
    summary = "Say when a file, a URL, a command or another piece of work reaches a state."

    def __init__(
        self,
        environments_base_url: str,
        *,
        fetch: Fetch | None = None,
        client: EnvironmentsClient | None = None,
        audience: str = AUDIENCE,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.base_url = environments_base_url.rstrip("/")
        self.audience = audience
        self._fetch = fetch
        self._override = client
        self._sleep = sleep

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="watch.command",
                title="Repeat a workspace command until it says yes",
                description=(
                    "Run a command in this session's sandbox every few seconds until it "
                    "exits 0 or its output matches, or the watch expires."
                ),
                risk="execute",
                covers=("watch.command",),
            ),
        )

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:
        if context.work is None:
            return Availability(state=State.not_configured, detail="no work registry this turn")
        watching = sum(
            1 for record in context.work.running(context.session_id) if record.kind is Kind.watch
        )
        detail = f"{watching} watching" if watching else "nothing watched"
        return Availability(state=State.ready, detail=detail)

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        registry = context.work
        if registry is None:
            return ()
        timing = {
            "every_seconds": number_schema()
            .optional()
            .describe(
                f"Seconds between checks; {DEFAULT_EVERY_SECONDS:.0f} by default, "
                f"{MIN_EVERY_SECONDS:.0f} to {MAX_EVERY_SECONDS:.0f}."
            ),
            "for_seconds": number_schema()
            .optional()
            .describe(
                f"How long to keep watching; {DEFAULT_FOR_SECONDS:.0f} by default, "
                f"{MAX_FOR_SECONDS:.0f} at most. Expiry is a notice, not a failure."
            ),
            "wake": boolean_schema()
            .optional()
            .describe("Open a turn when it fires or expires and nobody is talking. Default true."),
            "objective": string_schema().describe(
                "What this watch is for, in a sentence. It is what the live block and the "
                "notice say, so write it for a person."
            ),
            "pattern": string_schema()
            .optional()
            .describe("A regular expression to fire on, instead of existence, 200 or exit 0."),
        }

        async def run_start(run: RunContext[Any]) -> dict[str, Any]:
            return await self._start(run.ctx, registry, dict(run.input))

        async def run_command(run: RunContext[Any]) -> dict[str, Any]:
            return await self._command(run.ctx, registry, dict(run.input))

        return (
            define_operation(
                {
                    "name": "watch.start",
                    "description": (
                        "Say when a workspace file exists or matches, a public URL answers "
                        "or matches, or another piece of work finishes. Give exactly one of "
                        "path, url or work_id. Returns a handle at once; a notice arrives "
                        "when it fires or expires (watch, wait for, monitor, when, notify)."
                    ),
                    "input": object_schema(
                        {
                            "path": string_schema()
                            .optional()
                            .describe("A workspace file, relative to this session."),
                            "url": string_schema()
                            .optional()
                            .describe("A public HTTPS address to GET on each check."),
                            "expect_status": integer_schema()
                            .optional()
                            .describe(
                                f"The status that counts as fired; {DEFAULT_STATUS} by default."
                            ),
                            "work_id": string_schema()
                            .optional()
                            .describe("A running piece of work, by the id that started it."),
                            **timing,
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": run_start,
                }
            ),
            define_operation(
                {
                    "name": "watch.command",
                    "description": (
                        "Run a workspace command every few seconds until it exits 0 or its "
                        "output matches, then say so. One approval covers every run. Returns "
                        "a handle at once (watch, poll, until, wait for a command)."
                    ),
                    "input": object_schema(
                        {
                            "command": string_schema().describe(
                                "The command, run in this session's subtree."
                            ),
                            **timing,
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": run_command,
                }
            ),
        )

    # ------------------------------------------------------------------ handlers

    async def _start(
        self, context: PackContext, registry: Registry, raw: dict[str, Any]
    ) -> dict[str, Any]:
        named = {key: str(raw[key]) for key in ("path", "url", "work_id") if raw.get(key)}
        if len(named) != 1:
            return _refusal("invalid", "name exactly one of path, url or work_id")
        try:
            pattern = compile_pattern(str(raw.get("pattern") or ""))
        except ValueError as exc:
            return _refusal("invalid", str(exc))
        source, target = next(iter(named.items()))
        if source == "work_id":
            probe = _work_probe(registry, target)
        elif source == "url":
            probe = self._url_probe(target, raw, pattern)
        else:
            probe = self._file_probe(context, target, pattern)
        if isinstance(probe, dict):
            return probe
        return _begin(context, registry, probe, raw, sleep=self._sleep)

    async def _command(
        self, context: PackContext, registry: Registry, raw: dict[str, Any]
    ) -> dict[str, Any]:
        command = str(raw.get("command") or "").strip()
        if not command:
            return _refusal("invalid", "say which command to run")
        if not context.workspace_environment_id:
            return _refusal("not_connected", NO_WORKSPACE)
        try:
            pattern = compile_pattern(str(raw.get("pattern") or ""))
        except ValueError as exc:
            return _refusal("invalid", str(exc))
        client = self._client(context)

        async def probe() -> Check:
            return await _command_check(
                client, context.workspace_environment_id, context.workspace_path, command, pattern
            )

        return _begin(context, registry, probe, raw, sleep=self._sleep)

    def _url_probe(
        self, target: str, raw: dict[str, Any], pattern: re.Pattern[str] | None
    ) -> Probe | dict[str, Any]:
        fetch = self._fetch
        if fetch is None:
            return _refusal("not_configured", "URL watches are not configured here")
        try:
            assert_public_https(target)
        except LucyError:
            return _refusal("refused", ADDRESS_REFUSED)
        expected = raw.get("expect_status")
        status = int(expected) if isinstance(expected, int) else DEFAULT_STATUS

        async def url_probe() -> Check:
            return await _url_check(fetch, target, status, pattern)

        return url_probe

    def _file_probe(
        self, context: PackContext, target: str, pattern: re.Pattern[str] | None
    ) -> Probe | dict[str, Any]:
        if not context.workspace_environment_id:
            return _refusal("not_connected", NO_WORKSPACE)
        try:
            absolute = confined_path(context, target)
        except ConfinementError as exc:
            return _refusal("invalid", str(exc))
        client = self._client(context)
        environment = context.workspace_environment_id

        async def file_probe() -> Check:
            return await _file_check(client, environment, absolute, pattern)

        return file_probe

    def _client(self, context: PackContext) -> EnvironmentsClient:
        return self._override or HttpEnvironmentsClient(
            context.http, self.base_url, audience=self.audience
        )


# --------------------------------------------------------------------------------------
# One check of each kind. Each returns what the live block shows while waiting, and what
# the result carries once it fired. None of them raises for "not yet"; a raise is a failed
# check, which the watch tolerates a few times and then reports as a broken probe.
# --------------------------------------------------------------------------------------


async def _file_check(
    client: EnvironmentsClient,
    environment: str,
    path: str,
    pattern: re.Pattern[str] | None,
) -> Check:
    try:
        head = await client.read(environment, path, max_bytes=MAX_BODY)
    except AbsentError:
        return Check(fired=False, detail="no file yet")
    facts = {"size": head.size}
    if pattern is None:
        excerpt = "" if head.binary else excerpt_around(head.content, None)
        return Check(fired=True, detail="file exists", excerpt=excerpt, facts=facts)
    if head.binary:
        return Check(fired=False, detail=BINARY_FILE, facts=facts)
    found = pattern.search(head.content)
    body = head.content
    if found is None and head.truncated:
        # The verdict of a long log is at the end, and the head window did not reach it.
        offset = max(0, head.size - MAX_BODY)
        tail = await read_from(client, environment, path, offset, max_bytes=MAX_BODY)
        body = tail.content
        found = pattern.search(body)
    if found is None:
        return Check(fired=False, detail=f"no match in {head.size:,} bytes", facts=facts)
    return Check(fired=True, detail="matched", excerpt=excerpt_around(body, found), facts=facts)


async def _url_check(
    fetch: Fetch, url: str, expected: int, pattern: re.Pattern[str] | None
) -> Check:
    status, body = await fetch(url)
    facts = {"status": status}
    if status != expected:
        return Check(fired=False, detail=f"answered {status}", facts=facts)
    if pattern is None:
        return Check(
            fired=True, detail=f"answered {status}", excerpt=excerpt_around(body, None), facts=facts
        )
    found = pattern.search(body)
    if found is None:
        return Check(fired=False, detail=f"answered {status}, no match", facts=facts)
    return Check(fired=True, detail="matched", excerpt=excerpt_around(body, found), facts=facts)


def _work_check(registry: Registry, work_id: str) -> Check:
    state = registry.state_of(work_id)
    if not state.finished:
        return Check(fired=False, detail="still running")
    return Check(fired=True, detail=state.value, facts={"work_state": state.value})


async def _command_check(
    client: EnvironmentsClient,
    environment: str,
    cwd: str,
    command: str,
    pattern: re.Pattern[str] | None,
) -> Check:
    ran = await client.run(
        environment,
        command,
        cwd=cwd,
        timeout_ms=COMMAND_TIMEOUT_MS,
        max_output_bytes=DEFAULT_OUTPUT_BYTES,
    )
    if ran.timed_out:
        return Check(fired=False, detail="command timed out")
    facts = {"exit_code": ran.exit_code}
    if pattern is None:
        if ran.exit_code != 0:
            return Check(fired=False, detail=f"exit {ran.exit_code}", facts=facts)
        return Check(
            fired=True, detail="exit 0", excerpt=excerpt_around(ran.output, None), facts=facts
        )
    found = pattern.search(ran.output)
    if found is None and ran.output_truncated_bytes:
        # Only the head came back, and a verdict printed last is in the part that did not.
        detail = (
            f"exit {ran.exit_code}, no match; {ran.output_truncated_bytes:,} later bytes unread"
        )
        return Check(fired=False, detail=detail, facts=facts)
    if found is None:
        return Check(fired=False, detail=f"exit {ran.exit_code}, no match", facts=facts)
    return Check(
        fired=True, detail="matched", excerpt=excerpt_around(ran.output, found), facts=facts
    )


# --------------------------------------------------------------------------------------
# Starting, and refusing.
# --------------------------------------------------------------------------------------


def _begin(
    context: PackContext, registry: Registry, probe: Probe, raw: dict[str, Any], *, sleep: Sleep
) -> dict[str, Any]:
    objective = " ".join(str(raw.get("objective") or "").split())
    if not objective:
        return _refusal("invalid", "say what the watch is for, in a sentence")
    every = clamp_every(raw.get("every_seconds"))
    lifetime = clamp_for(raw.get("for_seconds"))
    wake = raw.get("wake", True)
    wake = wake if isinstance(wake, bool) else True
    work_id = new_id()

    def progress(note: str) -> None:
        registry.progress(work_id, note)

    try:
        handle = registry.start(
            watch(probe, every_seconds=every, progress=progress, sleep=sleep),
            Brief(
                session_id=context.session_id,
                kind=Kind.watch,
                role="watch",
                objective=objective,
                timeout_seconds=lifetime,
                account_id=context.account_id,
                wake=wake,
            ),
            work_id=work_id,
        )
    except AtCapacityError as exc:
        return _refusal("busy", str(exc))
    return {
        "id": handle.id,
        "state": "running",
        "every_seconds": every,
        "for_seconds": lifetime,
        "wake": wake,
        "advice": (
            "Watching. Carry on, or finish your answer; a notice arrives when it fires or "
            "expires" + (", and the session is woken if nobody is talking." if wake else ".")
        ),
    }


def _refusal(status: str, message: str) -> dict[str, Any]:
    return {"status": status, "message": message}


__all__ = ["COMMAND_TIMEOUT_MS", "MAX_BODY", "Fetch", "WatchPack", "httpx_fetch"]
