"""The watch capability, as the model calls it.

Driven through the real weftai runtime over the real operations, like the work pack's
tests, because what matters is what a call to the handler would miss: that `watch.command`
is a write and is refused in a read-only turn, that every refusal is a result the model can
act on, and that a watch that fires ends up readable through `work.result` like any other
piece of work.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from lucy_api.clients.environments import Environment, FakeEnvironmentsClient, FileText, Ran
from lucy_api.clients.errors import AbsentError
from lucy_api.net.ssrf import REFUSED as ADDRESS_REFUSED
from lucy_api.packs.base import State as PackState
from lucy_api.packs.context import PackContext, SilentTokens
from lucy_api.packs.http import NullHttp
from lucy_api.packs.registry import build_registry, build_runtime
from lucy_api.packs.watch import MAX_BODY, NO_WORKSPACE, WatchPack, httpx_fetch
from lucy_api.prompt.docs import capability_doc
from lucy_api.work import Brief, Kind, Registry, State
from lucy_api.work.watch import MAX_EVERY_SECONDS, MIN_EVERY_SECONDS

SESSION = "ses_watch"
ENV = "env-1"
ROOT = f"sessions/{SESSION}"
START = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Workspace(FakeEnvironmentsClient):
    """The fake, with the one behaviour a file watch depends on: a missing file is absent."""

    async def read(
        self, environment_id: str, path: str, *, offset: int = 0, max_bytes: int | None = None
    ) -> FileText:
        if (environment_id, path) not in self.contents:
            raise AbsentError(404, "no such file")
        return await super().read(environment_id, path, offset=offset, max_bytes=max_bytes)


class Sleeper:
    """Sleeps that take no time, and let the test decide when the next check happens."""

    def __init__(self) -> None:
        self.slept: list[float] = []
        self.gate = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        await self.gate.wait()
        self.gate.clear()

    async def tick(self) -> None:
        """Let one sleeping watch check again."""
        self.gate.set()
        await settled()


class Web:
    """A fetch that answers from a script; the address is recorded, never dialled."""

    def __init__(self, status: int = 503, body: str = "") -> None:
        self.status = status
        self.body = body
        self.asked: list[str] = []

    async def __call__(self, url: str) -> tuple[int, str]:
        self.asked.append(url)
        return self.status, self.body


async def settled() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


def a_context(registry: Registry | None, *, workspace: bool = True) -> PackContext:
    return PackContext(
        account_id="acc_1",
        profile="personal",
        session_id=SESSION,
        http=NullHttp(),
        tokens=SilentTokens(),
        work=registry,
        permission_mode="auto",
        workspace_environment_id=ENV if workspace else "",
        workspace_path=ROOT if workspace else "",
    )


def a_pack(
    fake: FakeEnvironmentsClient | None = None,
    *,
    fetch: Web | None = None,
    sleep: Sleeper | None = None,
) -> WatchPack:
    return WatchPack(
        "https://workspace.test",
        client=fake or Workspace(),
        fetch=fetch,
        sleep=sleep or Sleeper(),
    )


async def run(
    pack: WatchPack, plan: dict[str, Any], context: PackContext, *, writes: bool = False
) -> Any:
    runtime = build_runtime(build_registry(pack.operations(context)))
    return await runtime.execute(plan, {"ctx": context, "allowWrites": writes})


def step(result: Any, index: int = 0) -> Any:
    return result["steps"][index]["data"]


def start(inputs: dict[str, Any], *, op: str = "watch.start") -> dict[str, Any]:
    body = {"objective": "Say when it lands", **inputs}
    return {"steps": [{"id": "w", "op": op, "input": body}]}


# --------------------------------------------------------------------------------------
# The capability
# --------------------------------------------------------------------------------------


async def test_it_is_ready_whenever_there_is_a_registry_and_counts_what_it_watches() -> None:
    pack = a_pack()
    registry = Registry(now=Clock())
    assert (await pack.probe(a_context(None))).state is PackState.not_configured
    assert pack.operations(a_context(None)) == ()
    assert (await pack.probe(a_context(registry))).detail == "nothing watched"

    handle = registry.start(
        asyncio.Event().wait(),
        Brief(session_id=SESSION, kind=Kind.watch, role="watch", objective="x"),
    )
    await settled()
    assert (await pack.probe(a_context(registry))).detail == "1 watching"
    registry.cancel(handle.id)
    await settled()


def test_its_page_and_its_one_permission() -> None:
    pack = a_pack()
    assert pack.docs == capability_doc("watch")
    assert pack.setup() is None
    (permission,) = pack.permissions()
    assert permission.id == "watch.command"
    assert permission.risk == "execute"
    assert permission.covers == ("watch.command",)


# --------------------------------------------------------------------------------------
# Refusals are results
# --------------------------------------------------------------------------------------


async def test_naming_no_source_or_two_is_refused_in_words() -> None:
    pack = a_pack()
    context = a_context(Registry(now=Clock()))

    none = step(await run(pack, start({}), context))
    two = step(await run(pack, start({"path": "a", "work_id": "wrk_1"}), context))

    assert none == {"status": "invalid", "message": "name exactly one of path, url or work_id"}
    assert two["status"] == "invalid"


async def test_a_bad_pattern_an_unknown_work_id_and_a_missing_workspace_each_say_so() -> None:
    pack = a_pack()
    context = a_context(Registry(now=Clock()))

    pattern = step(await run(pack, start({"path": "a", "pattern": "(oops"}), context))
    unknown = step(await run(pack, start({"work_id": "wrk_nobody"}), context))
    nowhere = step(
        await run(pack, start({"path": "a"}), a_context(Registry(now=Clock()), workspace=False))
    )
    outside = step(await run(pack, start({"path": "../../etc/passwd"}), context))

    assert pattern["status"] == "invalid"
    assert "regular expression" in pattern["message"]
    assert unknown["status"] == "unknown"
    assert "not something this session started" in unknown["message"]
    assert nowhere == {"status": "not_connected", "message": NO_WORKSPACE}
    assert outside["status"] == "invalid"
    assert "outside this session" in outside["message"]


async def test_an_address_that_is_not_public_https_is_refused_without_naming_it() -> None:
    pack = a_pack(fetch=Web())
    context = a_context(Registry(now=Clock()))

    plain = step(await run(pack, start({"url": "http://example.com/status"}), context))
    private = step(await run(pack, start({"url": "https://127.0.0.1:8001/x"}), context))

    assert plain == {"status": "refused", "message": ADDRESS_REFUSED}
    assert private == {"status": "refused", "message": ADDRESS_REFUSED}
    assert "8001" not in str(private)


async def test_a_deployment_without_a_fetch_says_url_watches_are_not_configured() -> None:
    pack = a_pack(fetch=None)
    context = a_context(Registry(now=Clock()))

    refused = step(await run(pack, start({"url": "https://example.com/status"}), context))

    assert refused["status"] == "not_configured"


async def test_an_empty_objective_and_a_full_registry_are_refused_too() -> None:
    pack = a_pack()
    empty = step(
        await run(
            pack,
            {"steps": [{"id": "w", "op": "watch.start", "input": {"path": "a", "objective": " "}}]},
            a_context(Registry(now=Clock())),
        )
    )
    full = step(
        await run(pack, start({"path": "a"}), a_context(Registry(now=Clock(), max_concurrent=0)))
    )

    assert empty["status"] == "invalid"
    assert "sentence" in empty["message"]
    assert full["status"] == "busy"
    assert "already running" in full["message"]


async def test_the_command_form_needs_a_command_a_workspace_and_a_write_turn() -> None:
    pack = a_pack()
    registry = Registry(now=Clock())

    blank = step(
        await run(
            pack, start({"command": "  "}, op="watch.command"), a_context(registry), writes=True
        )
    )
    nowhere = step(
        await run(
            pack,
            start({"command": "make"}, op="watch.command"),
            a_context(registry, workspace=False),
            writes=True,
        )
    )
    bad = step(
        await run(
            pack,
            start({"command": "make", "pattern": "("}, op="watch.command"),
            a_context(registry),
            writes=True,
        )
    )
    readonly = await run(pack, start({"command": "make"}, op="watch.command"), a_context(registry))

    assert blank == {"status": "invalid", "message": "say which command to run"}
    assert nowhere == {"status": "not_connected", "message": NO_WORKSPACE}
    assert bad["status"] == "invalid"
    assert readonly["ok"] is False
    assert readonly["steps"] == [], "a write in a read-only turn does not run"
    assert readonly["issues"][0]["code"] == "step.write_not_allowed"


# --------------------------------------------------------------------------------------
# Watching a file
# --------------------------------------------------------------------------------------


async def test_a_file_watch_fires_when_the_file_appears_and_the_result_is_readable() -> None:
    fake = Workspace()
    fake.seed(Environment(ENV, "Conversation", profile="personal"))
    sleeper = Sleeper()
    pack = a_pack(fake, sleep=sleeper)
    clock = Clock()
    registry = Registry(now=clock)
    context = a_context(registry)

    started = step(await run(pack, start({"path": "out/export.csv", "every_seconds": 7}), context))
    assert started["state"] == "running"
    assert started["every_seconds"] == 7.0
    assert started["wake"] is True
    assert "woken" in started["advice"]
    await settled()

    record = registry.running(SESSION)[0]
    assert record.kind is Kind.watch
    assert record.account_id == "acc_1"
    assert record.wake is True
    assert record.progress == "checked 1x, no file yet; next in 7s"

    fake.contents[(ENV, f"{ROOT}/out/export.csv")] = "id,name\n1,a\n"
    await sleeper.tick()
    await settled()

    result = registry.result(started["id"])
    assert result.state is State.succeeded
    assert result.payload == {
        "fired": True,
        "checks": 2,
        "excerpt": "id,name\n1,a\n",
        "size": 12,
    }
    assert sleeper.slept == [7.0]


async def test_a_file_watch_with_a_pattern_reads_the_tail_of_a_long_log() -> None:
    fake = Workspace()
    fake.seed(
        Environment(ENV, "Conversation", profile="personal"),
        files=((f"{ROOT}/build.log", "x" * (MAX_BODY + 10) + "\nRESULT: green\n"),),
    )
    sleeper = Sleeper()
    pack = a_pack(fake, sleep=sleeper)
    registry = Registry(now=Clock())

    started = step(
        await run(
            pack, start({"path": "build.log", "pattern": r"RESULT: (\w+)"}), a_context(registry)
        )
    )
    await settled()

    result = registry.result(started["id"])
    assert result.payload["fired"] is True
    assert "RESULT: green" in result.payload["excerpt"]
    assert result.payload["size"] == MAX_BODY + 10 + len("\nRESULT: green\n")


async def test_a_file_watch_with_a_pattern_waits_while_there_is_no_match() -> None:
    fake = Workspace()
    fake.seed(
        Environment(ENV, "Conversation", profile="personal"),
        files=((f"{ROOT}/log", "still going"),),
    )
    sleeper = Sleeper()
    pack = a_pack(fake, sleep=sleeper)
    registry = Registry(now=Clock())

    started = step(await run(pack, start({"path": "log", "pattern": "done"}), a_context(registry)))
    await settled()

    record = registry.running(SESSION)[0]
    assert record.progress == "checked 1x, no match in 11 bytes; next in 15s"
    registry.cancel(started["id"])
    await settled()
    assert registry.result(started["id"]).state is State.cancelled


# --------------------------------------------------------------------------------------
# Watching an address, and another piece of work
# --------------------------------------------------------------------------------------


async def test_a_url_watch_fires_on_the_expected_status_and_carries_it() -> None:
    web = Web(status=503)
    sleeper = Sleeper()
    pack = a_pack(fetch=web, sleep=sleeper)
    registry = Registry(now=Clock())

    started = step(
        await run(pack, start({"url": "https://example.com/ready"}), a_context(registry))
    )
    await settled()
    assert registry.running(SESSION)[0].progress == "checked 1x, answered 503; next in 15s"

    web.status, web.body = 200, "ok"
    await sleeper.tick()
    await settled()

    payload = registry.result(started["id"]).payload
    assert payload == {"fired": True, "checks": 2, "excerpt": "ok", "status": 200}
    assert web.asked == ["https://example.com/ready"] * 2


async def test_a_url_watch_with_a_pattern_and_a_status_of_its_own() -> None:
    web = Web(status=202, body='{"state": "pending"}')
    sleeper = Sleeper()
    pack = a_pack(fetch=web, sleep=sleeper)
    registry = Registry(now=Clock())

    started = step(
        await run(
            pack,
            start(
                {
                    "url": "https://example.com/job",
                    "expect_status": 202,
                    "pattern": '"state": "done"',
                }
            ),
            a_context(registry),
        )
    )
    await settled()
    assert (
        registry.running(SESSION)[0].progress == "checked 1x, answered 202, no match; next in 15s"
    )

    web.body = '{"state": "done"}'
    await sleeper.tick()
    await settled()

    payload = registry.result(started["id"]).payload
    assert payload["fired"] is True
    assert payload["status"] == 202
    assert '"state": "done"' in payload["excerpt"]


async def test_a_work_watch_fires_when_that_work_ends_however_it_ends() -> None:
    sleeper = Sleeper()
    pack = a_pack(sleep=sleeper)
    registry = Registry(now=Clock())
    gate = asyncio.Event()

    async def job() -> str:
        await gate.wait()
        return "done"

    other = registry.start(
        job(), Brief(session_id=SESSION, kind=Kind.job, role="export", objective="x")
    )
    started = step(
        await run(pack, start({"work_id": other.id, "wake": False}), a_context(registry))
    )
    assert started["wake"] is False
    assert started["advice"].endswith("expires.")
    await settled()
    assert registry.running(SESSION)[1].progress == "checked 1x, still running; next in 15s"

    gate.set()
    await settled()
    await sleeper.tick()
    await settled()

    payload = registry.result(started["id"]).payload
    assert payload == {"fired": True, "checks": 2, "excerpt": "", "work_state": "succeeded"}


# --------------------------------------------------------------------------------------
# Watching a command
# --------------------------------------------------------------------------------------


async def test_a_command_watch_fires_on_exit_zero_and_one_approval_covers_every_run() -> None:
    fake = Workspace()
    fake.seed(Environment(ENV, "Conversation", profile="personal"))
    fake.script(
        "make check", Ran(command="make check", exit_code=1, output="failing", state="idle")
    )
    sleeper = Sleeper()
    pack = a_pack(fake, sleep=sleeper)
    registry = Registry(now=Clock())

    started = step(
        await run(
            pack,
            start({"command": "make check", "every_seconds": 1}, op="watch.command"),
            a_context(registry),
            writes=True,
        )
    )
    assert started["every_seconds"] == MIN_EVERY_SECONDS
    await settled()
    assert (
        registry.running(SESSION)[0].progress
        == f"checked 1x, exit 1; next in {MIN_EVERY_SECONDS:.0f}s"
    )

    fake.script(
        "make check", Ran(command="make check", exit_code=0, output="all passed", state="idle")
    )
    await sleeper.tick()
    await settled()

    payload = registry.result(started["id"]).payload
    assert payload == {"fired": True, "checks": 2, "excerpt": "all passed", "exit_code": 0}
    assert len(fake.ran) == 2, "the command ran once per check, through one approval"


async def test_a_command_watch_with_a_pattern_ignores_the_exit_code_and_a_timeout_is_not_yet() -> (
    None
):
    fake = Workspace()
    fake.seed(Environment(ENV, "Conversation", profile="personal"))
    fake.script(
        "tail log", Ran(command="tail log", exit_code=None, timed_out=True, state="running")
    )
    sleeper = Sleeper()
    pack = a_pack(fake, sleep=sleeper)
    registry = Registry(now=Clock())

    started = step(
        await run(
            pack,
            start(
                {"command": "tail log", "pattern": "total:", "for_seconds": 10**7},
                op="watch.command",
            ),
            a_context(registry),
            writes=True,
        )
    )
    assert started["for_seconds"] == 3600.0
    await settled()
    assert registry.running(SESSION)[0].progress == "checked 1x, command timed out; next in 15s"

    fake.script(
        "tail log", Ran(command="tail log", exit_code=2, output="lines\nno verdict", state="idle")
    )
    await sleeper.tick()
    await settled()
    assert registry.running(SESSION)[0].progress == "checked 2x, exit 2, no match; next in 15s"

    fake.script(
        "tail log",
        Ran(command="tail log", exit_code=2, output="lines\ntotal: 41 passed", state="idle"),
    )
    await sleeper.tick()
    await settled()

    payload = registry.result(started["id"]).payload
    assert payload["fired"] is True
    assert payload["exit_code"] == 2
    assert "total: 41 passed" in payload["excerpt"]


async def test_the_interval_ceiling_holds_whatever_the_model_asks_for() -> None:
    pack = a_pack(sleep=Sleeper())
    registry = Registry(now=Clock())
    fake_workspace = Workspace()
    fake_workspace.seed(Environment(ENV, "Conversation", profile="personal"))

    started = step(
        await run(pack, start({"work_id": "x", "every_seconds": 10**6}), a_context(registry))
    )
    assert started["status"] == "unknown", (
        "an unknown work id is refused before any interval applies"
    )

    gate = asyncio.Event()
    other = registry.start(
        gate.wait(), Brief(session_id=SESSION, kind=Kind.job, role="j", objective="x")
    )
    started = step(
        await run(pack, start({"work_id": other.id, "every_seconds": 10**6}), a_context(registry))
    )
    assert started["every_seconds"] == MAX_EVERY_SECONDS
    gate.set()
    await settled()
    registry.cancel(started["id"])
    await settled()


# --------------------------------------------------------------------------------------
# The real fetch
# --------------------------------------------------------------------------------------


async def test_the_real_fetch_never_follows_a_redirect_and_bounds_the_body() -> None:
    import httpx

    seen: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), True))
        return httpx.Response(
            302, headers={"location": "https://elsewhere.test/"}, text="z" * (MAX_BODY + 5)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status, body = await httpx_fetch(client)("https://example.com/ready")

    assert status == 302
    assert len(body) == MAX_BODY
    assert seen == [("https://example.com/ready", True)]
