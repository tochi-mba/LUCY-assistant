"""Workspace tools stay inside the session subtree and expose every truncation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.environments import (
    Environment,
    FakeEnvironmentsClient,
    Ran,
    Readiness,
)
from lucy_api.clients.errors import DownstreamError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.packs.service import Capabilities
from lucy_api.packs.workspace import (
    MAX_TOOL_OUTPUT_CHARS,
    WORKSPACE_MARKDOWN,
    WorkspacePack,
    _optional_int,
)
from lucy_api.permissions.gate import Grant
from lucy_api.sessions.scope import SessionScope, WorkspaceScope
from lucy_api.work import Registry
from lucy_api.workspace.text import digest


def setup() -> tuple[FakeEnvironmentsClient, Capabilities, object]:
    fake = FakeEnvironmentsClient()
    fake.seed(
        Environment("env-1", "Conversation", profile="personal"),
        files=(("sessions/sess-a/readme.md", "hello world"),),
    )
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    context.grants["workspace.destroy"] = Grant("workspace.destroy", "allow", "*")
    return fake, capabilities, context


def test_workspace_needs_no_manual_setup() -> None:
    fake, _capabilities, _context = setup()
    pack = WorkspacePack("https://workspace.test", client=fake)

    assert pack.setup() is None
    assert pack.docs == WORKSPACE_MARKDOWN
    assert pack.permissions()[1].id == "workspace.destroy"
    assert pack.permissions()[1].covers == ("workspace.delete",)


async def test_workspace_without_an_attachment_has_no_tools() -> None:
    fake = FakeEnvironmentsClient()
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(account_id="acct-a", profile="personal", session_id="sess-a")
    )
    catalogue = await capabilities.probe(context)

    assert catalogue.bound[0].availability.state == "not_connected"
    assert capabilities.tools(catalogue, "sess-a")["tools"] == []


async def test_workspace_operations_are_prefixed_to_the_session_subtree() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    fake.script(
        "build",
        Ran(
            command="build",
            exit_code=0,
            output="x" * (MAX_TOOL_OUTPUT_CHARS + 10),
            output_dropped_bytes=7,
            state="idle",
        ),
    )

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "list", "op": "workspace.list", "input": {}},
                {
                    "id": "read",
                    "op": "workspace.read",
                    "input": {"path": "readme.md"},
                },
                {
                    "id": "write",
                    "op": "workspace.write",
                    "input": {"path": "notes.txt", "content": "one"},
                },
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {"path": "notes.txt", "old_string": "one", "new_string": "two"},
                },
                {
                    "id": "grep",
                    "op": "workspace.grep",
                    "input": {"pattern": "two"},
                },
                {
                    "id": "move",
                    "op": "workspace.move",
                    "input": {"source": "notes.txt", "destination": "done.txt"},
                },
                {"id": "run", "op": "workspace.run", "input": {"command": "build"}},
                {
                    "id": "delete",
                    "op": "workspace.delete",
                    "input": {"path": "done.txt"},
                },
            ]
        },
        context,
    )

    assert not result["issues"]
    assert fake.wrote == [("env-1", "sessions/sess-a/notes.txt")]
    assert fake.ran[0][0:2] == ("env-1", "build")
    assert "readme.md" in str(result)
    assert "sessions/sess-a/readme.md" not in str(result)
    assert "17 output characters or bytes omitted" in str(result)
    assert ("env-1", "sessions/sess-a/done.txt") not in fake.contents


async def test_deleting_in_auto_still_asks_unless_destroy_is_granted() -> None:
    _fake, capabilities, context = setup()
    context.grants.pop("workspace.destroy", None)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "delete", "op": "workspace.delete", "input": {"path": "readme.md"}},
            ]
        },
        context,
    )

    assert result["issues"][0]["code"] == "permission_required"
    assert result["issues"][0]["permission"] == "workspace.destroy"
    assert "destructive" in result["issues"][0]["message"]


async def test_workspace_escape_is_refused_before_the_client_is_called() -> None:
    _fake, capabilities, context = setup()
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "escape",
                    "op": "workspace.read",
                    "input": {"path": "../../other-session/secret"},
                }
            ]
        },
        context,
    )

    assert "outside this session" in str(result)


async def test_an_absolute_workspace_path_is_refused() -> None:
    _fake, capabilities, context = setup()
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "abs", "op": "workspace.read", "input": {"path": "/etc/passwd"}},
            ]
        },
        context,
    )
    assert "relative to this session" in str(result)


async def test_workspace_edit_patch_and_list_stay_inside_the_session() -> None:
    _fake, capabilities, context = setup()
    await capabilities.probe(context)
    written = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "write",
                    "op": "workspace.write",
                    "input": {"path": "notes.txt", "content": "alpha"},
                },
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {
                        "path": "notes.txt",
                        "old_string": "alpha",
                        "new_string": "beta",
                    },
                },
                {
                    "id": "patch",
                    "op": "workspace.patch",
                    "input": {"path": "notes.txt", "patch": "gamma"},
                },
                {"id": "list", "op": "workspace.list", "input": {}},
            ]
        },
        context,
    )
    assert not written["issues"]
    listed = written["steps"][3]["data"]
    assert any(item["name"] == "notes.txt" for item in listed)


async def test_workspace_without_a_usable_token_is_unavailable() -> None:
    capabilities = Capabilities([WorkspacePack("https://workspace.test")])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
        )
    )
    catalogue = await capabilities.probe(context)
    row = capabilities.listings(catalogue)[0]
    assert row["state"] == "unavailable"


async def test_a_workspace_that_is_not_ready_stays_unavailable() -> None:
    fake, capabilities, context = setup()
    fake.readiness = Readiness(ready=False, sandbox_tier="container")
    catalogue = await capabilities.probe(context)
    row = capabilities.listings(catalogue)[0]
    assert row["state"] == "unavailable"
    assert "not ready" in row["detail"]


async def test_workspace_probe_names_an_exchange_failure() -> None:
    class Boom:
        async def ready(self) -> object:
            raise ExchangeError("no grant")

    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=Boom())])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
        )
    )
    catalogue = await capabilities.probe(context)
    assert capabilities.listings(catalogue)[0]["state"] == "unavailable"


async def test_workspace_probe_names_a_downstream_outage() -> None:
    class Boom:
        async def ready(self) -> object:
            raise DownstreamError("workspace", 503)

    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=Boom())])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
        )
    )
    catalogue = await capabilities.probe(context)
    assert "could not be reached" in capabilities.listings(catalogue)[0]["detail"]


async def test_workspace_probe_names_a_transport_outage() -> None:
    class Boom:
        async def ready(self) -> object:
            raise TransportError("down", audience="workspace")

    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=Boom())])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
        )
    )
    catalogue = await capabilities.probe(context)
    assert "could not be reached" in capabilities.listings(catalogue)[0]["detail"]


def test_workspace_has_no_docs_page() -> None:
    assert WorkspacePack("https://workspace.test").docs == WORKSPACE_MARKDOWN
    assert _optional_int(True) is None
    assert _optional_int(8) == 8


async def test_accept_edits_allows_file_changes_but_still_asks_before_a_command() -> None:
    fake, capabilities, _auto = setup()
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="accept_edits",
        )
    )
    await capabilities.probe(context)

    written = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "write",
                    "op": "workspace.write",
                    "input": {"path": "notes.txt", "content": "one"},
                }
            ]
        },
        context,
    )
    running = await capabilities.execute(
        {"steps": [{"id": "run", "op": "workspace.run", "input": {"command": "build"}}]},
        context,
    )

    assert not written["issues"]
    assert fake.wrote == [("env-1", "sessions/sess-a/notes.txt")]
    assert running["issues"][0]["code"] == "permission_required"
    assert running["issues"][0]["permission"] == "workspace.run"


async def test_workspace_read_numbers_lines_and_returns_fingerprints() -> None:
    _fake, capabilities, context = setup()
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "read", "op": "workspace.read", "input": {"path": "readme.md"}},
            ]
        },
        context,
    )
    payload = result["steps"][0]["data"]
    assert payload["content"].startswith("1\t")
    assert "hello world" in payload["content"]
    assert payload["file_fingerprint"]
    assert "showing lines 1-1 of 1" in payload["notice"]


async def test_a_stale_fingerprint_refuses_an_edit_and_a_write() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    await fake.write("env-1", "sessions/sess-a/notes.txt", "one")
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {
                        "path": "notes.txt",
                        "old_string": "one",
                        "new_string": "two",
                        "if_match": "deadbeefdeadbeef",
                    },
                },
                {
                    "id": "write",
                    "op": "workspace.write",
                    "input": {
                        "path": "notes.txt",
                        "content": "three",
                        "if_match": "deadbeefdeadbeef",
                    },
                },
            ]
        },
        context,
    )
    assert result["steps"][0]["data"]["replaced"] is False
    assert "changed since you read it" in result["steps"][0]["data"]["notice"]
    assert result["steps"][1]["data"]["written"] is False
    assert fake.contents[("env-1", "sessions/sess-a/notes.txt")] == "one"


async def test_an_invalid_json_write_is_refused_before_the_file_changes() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "write",
                    "op": "workspace.write",
                    "input": {"path": "data.json", "content": "{"},
                }
            ]
        },
        context,
    )
    assert result["steps"][0]["data"]["written"] is False
    assert "JSON" in result["steps"][0]["data"]["notice"]
    assert ("env-1", "sessions/sess-a/data.json") not in fake.contents


async def test_a_matching_fingerprint_allows_a_write() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    await fake.write("env-1", "sessions/sess-a/notes.txt", "one")
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "write",
                    "op": "workspace.write",
                    "input": {
                        "path": "notes.txt",
                        "content": "two",
                        "if_match": digest("one"),
                    },
                }
            ]
        },
        context,
    )
    assert result["steps"][0]["data"]["written"] is True
    assert fake.contents[("env-1", "sessions/sess-a/notes.txt")] == "two"


async def test_a_binary_file_is_refused_on_read_and_edit() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    fake.contents[("env-1", "sessions/sess-a/blob.bin")] = "a\0b"
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "read", "op": "workspace.read", "input": {"path": "blob.bin"}},
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {
                        "path": "blob.bin",
                        "old_string": "a",
                        "new_string": "b",
                    },
                },
            ]
        },
        context,
    )
    assert result["steps"][0]["data"]["binary"] is True
    assert result["steps"][1]["data"]["replaced"] is False


async def test_an_ambiguous_edit_names_the_lines() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    await fake.write("env-1", "sessions/sess-a/notes.txt", "one\nmid\none\n")
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {
                        "path": "notes.txt",
                        "old_string": "one",
                        "new_string": "two",
                    },
                }
            ]
        },
        context,
    )
    assert "lines: 1, 3" in result["steps"][0]["data"]["notice"]


async def test_a_stale_patch_is_refused() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    await fake.write("env-1", "sessions/sess-a/notes.txt", "one")
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "patch",
                    "op": "workspace.patch",
                    "input": {
                        "path": "notes.txt",
                        "patch": "two",
                        "if_match": "deadbeefdeadbeef",
                    },
                }
            ]
        },
        context,
    )
    assert result["steps"][0]["data"]["replaced"] is False
    assert fake.contents[("env-1", "sessions/sess-a/notes.txt")] == "one"


async def test_an_invalid_python_edit_is_refused() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    await fake.write("env-1", "sessions/sess-a/app.py", "x = 1\n")
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "edit",
                    "op": "workspace.edit",
                    "input": {
                        "path": "app.py",
                        "old_string": "x = 1",
                        "new_string": "def (",
                    },
                }
            ]
        },
        context,
    )
    assert result["steps"][0]["data"]["replaced"] is False
    assert "Python" in result["steps"][0]["data"]["notice"]


async def test_a_truncated_read_confesses_the_prefix_fingerprint() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)
    fake.contents[("env-1", "sessions/sess-a/long.txt")] = "abcdefghij"
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "read",
                    "op": "workspace.read",
                    "input": {"path": "long.txt", "max_bytes": 4},
                }
            ]
        },
        context,
    )
    assert "retrieved prefix" in result["steps"][0]["data"]["notice"]
    assert result["steps"][0]["data"]["truncated"] is True


async def test_a_long_command_returns_a_handle_when_the_model_asks_not_to_wait() -> None:
    fake, _capabilities, _context = setup()
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)], work=work)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    await capabilities.probe(context)
    fake.script("build", Ran(command="build", exit_code=0, output="ok", state="idle"))
    try:
        result = await capabilities.execute(
            {
                "steps": [
                    {
                        "id": "run",
                        "op": "workspace.run",
                        "input": {"command": "build", "wait": False},
                    }
                ]
            },
            context,
        )
        payload = result["steps"][0]["data"]
        assert payload["status"] == "running"
        assert payload["work_id"].startswith("wrk_")
        assert "work.check" in payload["notice"]
    finally:
        await work.shutdown()


async def test_a_command_that_outlives_the_wait_is_left_running() -> None:
    class SlowFake(FakeEnvironmentsClient):
        def __init__(self) -> None:
            super().__init__()
            self.gate = asyncio.Event()

        async def run(
            self,
            environment_id: str,
            command: str,
            *,
            cwd: str = ".",
            timeout_ms: int = 60_000,
            max_output_bytes: int = 64 * 1024,
        ) -> Ran:
            await self.gate.wait()
            return await super().run(
                environment_id,
                command,
                cwd=cwd,
                timeout_ms=timeout_ms,
                max_output_bytes=max_output_bytes,
            )

    fake = SlowFake()
    fake.seed(Environment("env-1", "Conversation", profile="personal"))
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)], work=work)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    await capabilities.probe(context)
    try:
        result = await capabilities.execute(
            {
                "steps": [
                    {
                        "id": "run",
                        "op": "workspace.run",
                        "input": {
                            "command": "sleep",
                            "wait": True,
                            "wait_seconds": 0,
                        },
                    }
                ]
            },
            context,
        )
        payload = result["steps"][0]["data"]
        assert payload["status"] == "running"
        assert "still running" in payload["notice"]
        assert payload["work_id"]
    finally:
        fake.gate.set()
        await work.shutdown()


async def test_a_full_work_registry_says_busy_instead_of_starting_a_twenty_first_command() -> None:
    fake, _capabilities, _context = setup()
    work = Registry(now=lambda: datetime.now(UTC), max_concurrent=0)
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)], work=work)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "run", "op": "workspace.run", "input": {"command": "build"}}]},
        context,
    )
    payload = result["steps"][0]["data"]
    assert payload["status"] == "busy"
    assert "already running" in payload["message"]


async def test_a_waited_command_returns_its_output_and_handle() -> None:
    fake, _capabilities, _context = setup()
    work = Registry(now=lambda: datetime.now(UTC))
    capabilities = Capabilities([WorkspacePack("https://workspace.test", client=fake)], work=work)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct-a",
            profile="personal",
            session_id="sess-a",
            workspace=WorkspaceScope("env-1", "sess-a", ready=True),
            permission_mode="auto",
        )
    )
    await capabilities.probe(context)
    fake.script("build", Ran(command="build", exit_code=0, output="ok", state="idle"))
    try:
        result = await capabilities.execute(
            {"steps": [{"id": "run", "op": "workspace.run", "input": {"command": "build"}}]},
            context,
        )
        payload = result["steps"][0]["data"]
        assert payload["exit_code"] == 0
        assert payload["output"] == "ok"
        assert payload["work_id"]
    finally:
        await work.shutdown()


def test_a_finished_command_keeps_a_non_object_result_reachable() -> None:
    from lucy_api.packs.workspace import _completed_command

    assert _completed_command({"exit_code": 0}, "wrk_1") == {"exit_code": 0, "work_id": "wrk_1"}
    assert _completed_command("done", "wrk_2") == {"work_id": "wrk_2", "result": "done"}
