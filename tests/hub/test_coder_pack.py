"""Claude Code delegation from the hub's side: three switches, a card every time, and
tracking that tells the truth about another program's work."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from lucy_api.clients.coder import CoderTask, FakeCoderClient, Readiness
from lucy_api.clients.errors import DownstreamError
from lucy_api.context.types import Trust
from lucy_api.packs import coder as coder_module
from lucy_api.packs.base import State
from lucy_api.packs.coder import NO_DIRECTORIES, PERSONS_SWITCH, CoderPack, _allowed
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.gate import Grant, once_key
from lucy_api.sessions.scope import SessionScope
from lucy_api.work import Kind, Registry

FOLDER = "C:/Users/them/code/tool"


def a_scope(mode: str = "auto") -> SessionScope:
    return SessionScope(
        account_id="acct_a", profile="personal", session_id="sess_a", permission_mode=mode
    )


def wired(
    *,
    base_url: str = "http://coder.test",
    on: bool = True,
    folders: tuple[str, ...] = (FOLDER,),
    level: str = "edits",
    work: Registry | None = None,
    mode: str = "auto",
) -> tuple[FakeCoderClient, Capabilities, Any]:
    fake = FakeCoderClient()
    capabilities = Capabilities([CoderPack(base_url, client=fake)], work=work)
    context = capabilities.context_for(a_scope(mode))
    context.policy = replace(
        context.policy,
        claude_code_delegation=on,
        claude_code_directories=folders,
        claude_code_run_level=level,
    )
    return fake, capabilities, context


def yes_to(context: Any, operation: str, arguments: dict[str, Any]) -> None:
    """The person's yes to one exact call, as an approved card leaves it."""
    context.grants[once_key(operation, arguments)] = Grant(
        "coder.delegate", "allow", "personal", source="person"
    )


def delegate(brief: str = "add a --version flag", directory: str = FOLDER) -> dict[str, Any]:
    return {
        "steps": [
            {
                "id": "go",
                "op": "coder.delegate",
                "input": {"brief": brief, "directory": directory, "title": "version flag"},
            }
        ]
    }


# -------------------------------------------------------------------------- the switches


async def test_no_bridge_url_means_the_capability_is_simply_absent() -> None:
    _fake, capabilities, context = wired(base_url="")
    catalogue = await capabilities.probe(context)
    bound = catalogue.get("coder")
    assert bound is not None
    assert bound.availability.state is State.not_configured


@pytest.mark.parametrize(
    ("on", "folders", "detail"),
    [(False, (FOLDER,), PERSONS_SWITCH), (True, (), NO_DIRECTORIES)],
)
async def test_the_persons_switches_disable_it_and_say_whose_they_are(
    on: bool, folders: tuple[str, ...], detail: str
) -> None:
    _fake, capabilities, context = wired(on=on, folders=folders)
    catalogue = await capabilities.probe(context)
    availability = catalogue.get("coder").availability
    assert availability.state is State.disabled
    assert availability.detail == detail
    assert "do not try" in detail


async def test_an_unreachable_or_unready_bridge_is_unavailable_with_its_reason() -> None:
    fake, capabilities, context = wired()
    fake.down = DownstreamError("down", status=503)
    assert (await capabilities.probe(context)).get("coder").availability.state is State.unavailable

    fake, capabilities, context = wired()
    fake.readiness = Readiness(ready=False, detail="Claude Code is not installed on this machine")
    availability = (await capabilities.probe(context)).get("coder").availability
    assert availability.state is State.unavailable
    assert "not installed" in availability.detail

    fake, capabilities, context = wired()
    fake.readiness = Readiness(ready=False)
    availability = (await capabilities.probe(context)).get("coder").availability
    assert availability.detail == "the Claude Code bridge is not ready"


async def test_everything_on_is_ready() -> None:
    _fake, capabilities, context = wired()
    availability = (await capabilities.probe(context)).get("coder").availability
    assert availability.state is State.ready


# -------------------------------------------------------------------------- the card


async def test_a_delegation_asks_every_time_even_in_auto() -> None:
    """Each card is a different brief about to act in the person's name on their machine:
    neither `auto` nor a standing yes covers the next one."""
    fake, capabilities, context = wired(mode="auto")
    context.grants["coder.delegate"] = Grant("coder.delegate", "allow", "personal")
    await capabilities.probe(context)

    result = await capabilities.execute(delegate(), context)

    assert result["issues"][0]["code"] == "permission_required"
    assert "needs your yes to it, whatever the mode" in result["issues"][0]["message"]
    assert fake.started == []


async def test_a_yes_to_one_task_starts_it_at_the_persons_level_and_tracks_it() -> None:
    work = Registry(now=lambda: datetime.now(UTC))
    fake, capabilities, context = wired(level="plan", work=work)
    plan = delegate()
    yes_to(context, "coder.delegate", plan["steps"][0]["input"])
    await capabilities.probe(context)
    try:
        result = await capabilities.execute(plan, context)
        data = result["steps"][0]["data"]
        assert fake.started == [
            {
                "brief": "add a --version flag",
                "directory": FOLDER,
                "run_level": "plan",
                "title": "version flag",
            }
        ]
        assert data["state"] == "running"
        assert data["work_id"].startswith("wrk_")
        assert "coder.read shows progress" in data["notice"]
        [record] = work.running("sess_a")
        assert record.kind is Kind.job
        assert record.role == "coder"
        assert record.wake is True
        assert record.timeout_seconds == coder_module.TASK_SECONDS
    finally:
        await work.shutdown()


# -------------------------------------------------------------------------- the folder


def test_a_folder_must_be_one_listed_and_a_prefix_is_not_one() -> None:
    listed = ("C:/repos", "D:\\work\\site\\")
    assert _allowed("C:/repos", listed) == "C:/repos"
    assert _allowed("c:\\Repos\\", listed) == "C:/repos", "case and separators normalise"
    assert _allowed("d:/WORK/site", listed) == "D:\\work\\site\\"
    assert _allowed("C:/repos-secret", listed) is None, "never a prefix match"
    assert _allowed("C:/repos/inner", listed) is None, "never a subfolder either"
    assert _allowed("", listed) is None


async def test_an_unlisted_folder_is_refused_before_the_bridge_hears_of_it() -> None:
    fake, capabilities, context = wired()
    plan = delegate(directory="C:/Windows")
    yes_to(context, "coder.delegate", plan["steps"][0]["input"])
    await capabilities.probe(context)

    result = await capabilities.execute(plan, context)

    data = result["steps"][0]["data"]
    assert data["status"] == "refused"
    assert "C:/Windows is not among the folders" in data["message"]
    assert FOLDER in data["message"]
    assert fake.started == []


async def test_an_empty_brief_is_refused_with_a_sentence() -> None:
    fake, capabilities, context = wired()
    plan = delegate(brief="   ")
    yes_to(context, "coder.delegate", plan["steps"][0]["input"])
    await capabilities.probe(context)
    data = (await capabilities.execute(plan, context))["steps"][0]["data"]
    assert data["status"] == "invalid"
    assert fake.started == []


# -------------------------------------------------------------------------- tracking


async def test_the_work_item_settles_when_the_bridge_says_the_turn_ended() -> None:
    fake = FakeCoderClient()
    running = CoderTask(
        id="tsk_1",
        title="t",
        brief="b",
        directory=FOLDER,
        run_level="edits",
        state="running",
        tool_uses=3,
        last_tool="Edit",
    )
    fake.seed(running)
    registry = Registry(now=lambda: datetime.now(UTC))
    seen: list[str] = []
    registry.progress = lambda _work_id, note: seen.append(note)  # type: ignore[method-assign]
    original_sleep = asyncio.sleep

    async def finish_after_one_poll(_seconds: float) -> None:
        fake.seed(replace(running, state="idle", result="done: added the flag", turns=1))
        await original_sleep(0)

    coder_module.asyncio.sleep = finish_after_one_poll  # type: ignore[assignment]
    try:
        payload = await coder_module._settled(fake, registry, "tsk_1", "wrk_1")
    finally:
        coder_module.asyncio.sleep = original_sleep  # type: ignore[assignment]
        await registry.shutdown()
    assert payload["state"] == "idle"
    assert payload["result"] == "done: added the flag"
    assert seen == ["3 tool uses, last: Edit"], "progress is the hub's sentence, not theirs"


async def test_a_task_that_already_ended_is_returned_without_a_work_item() -> None:
    work = Registry(now=lambda: datetime.now(UTC))
    fake, capabilities, context = wired(work=work)
    fake.seed(
        CoderTask(
            id="tsk_9", title="t", brief="b", directory=FOLDER, run_level="edits", state="idle"
        )
    )
    yes_to(context, "coder.message", {"task": "tsk_9", "text": "and a test"})
    await capabilities.probe(context)
    try:
        plan = {
            "steps": [
                {"id": "m", "op": "coder.message", "input": {"task": "tsk_9", "text": "and a test"}}
            ]
        }
        data = (await capabilities.execute(plan, context))["steps"][0]["data"]
        assert fake.messages == [("tsk_9", "and a test")]
        assert "work_id" not in data
        assert work.running("sess_a") == ()
    finally:
        await work.shutdown()


# -------------------------------------------------------------------------- reading


async def test_read_list_and_cancel_report_the_row() -> None:
    fake, capabilities, context = wired()
    fake.seed(
        CoderTask(
            id="tsk_1",
            title="t",
            brief="b",
            directory=FOLDER,
            run_level="edits",
            state="idle",
            detail="the turn is over",
            result="all done",
            advice="message it to resume",
            transcript_tail='{"type":"result"}',
        )
    )
    context.grants["coder.control"] = Grant("coder.control", "allow", "personal")
    await capabilities.probe(context)
    plan = {
        "steps": [
            {"id": "r", "op": "coder.read", "input": {"task": "tsk_1", "tail_chars": 4000}},
            {"id": "l", "op": "coder.list", "input": {}},
        ]
    }
    read, listed = (step["data"] for step in (await capabilities.execute(plan, context))["steps"])
    assert read["result"] == "all done"
    assert read["detail"] == "the turn is over"
    assert read["advice"] == "message it to resume"
    assert read["transcript_tail"] == '{"type":"result"}'
    assert listed["count"] == 1
    assert "transcript_tail" not in listed["tasks"][0]

    stopped = await capabilities.execute(
        {"steps": [{"id": "c", "op": "coder.cancel", "input": {"task": "tsk_1"}}]}, context
    )
    assert stopped["steps"][0]["data"]["state"] == "cancelled"
    assert fake.cancelled == ["tsk_1"]


async def test_missing_task_ids_and_texts_are_refused_with_sentences() -> None:
    _fake, capabilities, context = wired()
    context.grants["coder.control"] = Grant("coder.control", "allow", "personal")
    yes_to(context, "coder.message", {"task": "", "text": ""})
    await capabilities.probe(context)
    plan = {
        "steps": [
            {"id": "r", "op": "coder.read", "input": {"task": ""}},
            {"id": "c", "op": "coder.cancel", "input": {"task": ""}},
            {"id": "m", "op": "coder.message", "input": {"task": "", "text": ""}},
        ]
    }
    steps = (await capabilities.execute(plan, context))["steps"]
    assert [step["data"]["status"] for step in steps] == ["invalid", "invalid", "invalid"]


# -------------------------------------------------------------------------- the frame


def test_everything_it_returns_is_untrusted_and_there_is_no_connect_flow() -> None:
    pack = CoderPack("http://coder.test")
    assert pack.result_trust("coder.read", {}) is Trust.untrusted
    assert pack.setup() is None
    assert pack.docs is not None
    titles = {permission.id: permission for permission in pack.permissions()}
    assert titles["coder.delegate"].each_call is not None
    assert titles["coder.delegate"].each_call({}) is True
    assert titles["coder.control"].each_call is None


def test_tail_chars_reads_as_a_whole_number_or_nothing() -> None:
    assert coder_module._tail_chars(4000) == 4000
    assert coder_module._tail_chars("12") == 12
    assert coder_module._tail_chars("lots") == 0
    assert coder_module._tail_chars(None) == 0
    assert coder_module._tail_chars(-5) == 0
