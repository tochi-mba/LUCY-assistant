"""The Claude Code bridge, projected for the coder pack.

The bridge (``src/lucy_coder`` in this repository, host-run) owns the durable truth: task
rows, transcripts, the queue. This client carries its answers across with nothing invented:
a task is returned as the bridge said it, and the one shaping done here is typing the
fields a pack reads so a drifting bridge fails a test instead of a conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.transport import Sibling, field, flag, number, rows, segment, text

if TYPE_CHECKING:
    from lucy_api.packs.context import Http

SERVICE = "coder"
AUDIENCE = "coder-api"

LIVE_STATES = frozenset({"queued", "running"})


@dataclass(frozen=True, slots=True)
class CoderTask:
    """One delegation as the bridge reports it."""

    id: str
    title: str
    brief: str
    directory: str
    run_level: str
    state: str
    detail: str = ""
    result: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    tool_uses: int = 0
    last_tool: str = ""
    last_text: str = ""
    queued_messages: int = 0
    resumable: bool = False
    advice: str = ""
    transcript_tail: str = ""
    model: str = ""
    permission_denials: tuple[dict[str, str], ...] = ()
    """What its last turn was refused -- each a tool and a clipped input -- in `ask` mode,
    where a tool that needs permission is refused headless rather than prompted."""

    @property
    def live(self) -> bool:
        return self.state in LIVE_STATES


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a delegation would work right now, and the bridge's own sentence when not."""

    ready: bool
    detail: str = ""


class CoderClient(Protocol):
    """What the coder pack needs from the bridge."""

    async def ready(self) -> Readiness: ...

    async def start(
        self, *, brief: str, directory: str, run_level: str, title: str, model: str = ""
    ) -> CoderTask: ...

    async def get(self, task_id: str, *, tail_chars: int = 0) -> CoderTask: ...

    async def tasks(self) -> tuple[CoderTask, ...]: ...

    async def message(
        self, task_id: str, text: str, *, mode: str = "", allow_tools: tuple[str, ...] = ()
    ) -> CoderTask: ...

    async def cancel(self, task_id: str) -> CoderTask: ...


class HttpCoderClient:
    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def ready(self) -> Readiness:
        payload = await self._api.send("GET", "/healthy")
        return Readiness(ready=text(payload, "status") == "ok")

    async def start(
        self, *, brief: str, directory: str, run_level: str, title: str, model: str = ""
    ) -> CoderTask:
        body = {"brief": brief, "directory": directory, "run_level": run_level, "title": title}
        if model:
            body["model"] = model
        payload = await self._api.send("POST", "/v1/tasks", body=body)
        return _task(payload)

    async def get(self, task_id: str, *, tail_chars: int = 0) -> CoderTask:
        params = {"tail_chars": tail_chars} if tail_chars else None
        payload = await self._api.send(
            "GET", f"/v1/tasks/{segment(task_id)}", params=params, repeatable=True
        )
        return _task(payload)

    async def tasks(self) -> tuple[CoderTask, ...]:
        payload = await self._api.send("GET", "/v1/tasks")
        return tuple(_task(row) for row in rows(payload, "tasks"))

    async def message(
        self,
        task_id: str,
        text_body: str,
        *,
        mode: str = "",
        allow_tools: tuple[str, ...] = (),
    ) -> CoderTask:
        body: dict[str, Any] = {"text": text_body}
        if mode:
            body["mode"] = mode
        if allow_tools:
            body["allow_tools"] = list(allow_tools)
        payload = await self._api.send("POST", f"/v1/tasks/{segment(task_id)}/message", body=body)
        return _task(payload)

    async def cancel(self, task_id: str) -> CoderTask:
        payload = await self._api.send(
            "POST", f"/v1/tasks/{segment(task_id)}/cancel", body={}, repeatable=True
        )
        return _task(payload)


def _money(payload: Any, key: str) -> float:
    """A fractional field: `number` is for whole numbers and would eat the cents."""
    try:
        return float(field(payload, key))
    except (TypeError, ValueError):
        return 0.0


def _task(payload: Any) -> CoderTask:
    return CoderTask(
        id=text(payload, "id"),
        title=text(payload, "title"),
        brief=text(payload, "brief"),
        directory=text(payload, "directory"),
        run_level=text(payload, "run_level"),
        state=text(payload, "state"),
        detail=text(payload, "detail"),
        result=text(payload, "result"),
        turns=int(number(payload, "turns")),
        cost_usd=_money(payload, "cost_usd"),
        tool_uses=int(number(payload, "tool_uses")),
        last_tool=text(payload, "last_tool"),
        last_text=text(payload, "last_text"),
        queued_messages=int(number(payload, "queued_messages")),
        resumable=flag(payload, "resumable"),
        advice=text(payload, "advice"),
        transcript_tail=text(payload, "transcript_tail"),
        model=text(payload, "model"),
        permission_denials=tuple(
            {"tool": text(row, "tool"), "input": text(row, "input")}
            for row in rows(payload, "permission_denials")
            if isinstance(row, dict)
        ),
    )


class FakeCoderClient:
    """The bridge in memory, for the pack's tests: scripted states, recorded calls."""

    def __init__(self) -> None:
        self.readiness = Readiness(ready=True)
        self.tasks_by_id: dict[str, CoderTask] = {}
        self.started: list[dict[str, str]] = []
        self.messages: list[tuple[str, str]] = []
        self.follow_ups: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.down: Exception | None = None

    def seed(self, task: CoderTask) -> None:
        self.tasks_by_id[task.id] = task

    async def ready(self) -> Readiness:
        if self.down is not None:
            raise self.down
        return self.readiness

    async def start(
        self, *, brief: str, directory: str, run_level: str, title: str, model: str = ""
    ) -> CoderTask:
        if self.down is not None:
            raise self.down
        made = CoderTask(
            id=f"tsk_{len(self.started) + 1}",
            title=title or brief[:60],
            brief=brief,
            directory=directory,
            run_level=run_level,
            state="running",
            model=model,
        )
        self.started.append(
            {
                "brief": brief,
                "directory": directory,
                "run_level": run_level,
                "title": title,
                "model": model,
            }
        )
        self.tasks_by_id[made.id] = made
        return made

    async def get(self, task_id: str, *, tail_chars: int = 0) -> CoderTask:
        del tail_chars
        return self.tasks_by_id[task_id]

    async def tasks(self) -> tuple[CoderTask, ...]:
        return tuple(self.tasks_by_id.values())

    async def message(
        self,
        task_id: str,
        text_body: str,
        *,
        mode: str = "",
        allow_tools: tuple[str, ...] = (),
    ) -> CoderTask:
        self.messages.append((task_id, text_body))
        self.follow_ups.append({"task": task_id, "mode": mode, "allow_tools": allow_tools})
        return self.tasks_by_id[task_id]

    async def cancel(self, task_id: str) -> CoderTask:
        self.cancelled.append(task_id)
        ended = replace(self.tasks_by_id[task_id], state="cancelled")
        self.tasks_by_id[task_id] = ended
        return ended
