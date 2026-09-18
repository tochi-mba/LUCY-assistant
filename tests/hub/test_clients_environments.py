"""The workspace client drops the host path and never invents an exit code."""

from __future__ import annotations

import pytest

from lucy_api.clients.environments import (
    FakeEnvironmentsClient,
    HttpEnvironmentsClient,
    Ran,
    _environment,
    _exit_code,
)
from lucy_api.clients.testing import Answer, FakeHttp, problem


def test_the_host_workspace_path_never_survives_the_projection() -> None:
    environment = _environment(
        {
            "id": "env-1",
            "name": "lucy-ses",
            "workspace": "/home/app/envs/secret/workspace",
            "last_activity_at": "2026-09-17T12:00:00Z",
        }
    )
    assert environment.environment_id == "env-1"
    assert not hasattr(environment, "workspace")
    assert "secret" not in repr(environment)


def test_a_missing_exit_code_stays_missing() -> None:
    assert _exit_code({"exit_code": None}) is None
    assert _exit_code({"exit_code": 2}) == 2
    assert _exit_code({}) is None


async def test_readiness_treats_an_outage_as_not_ready_rather_than_raising() -> None:
    http = FakeHttp(problem(503, detail="sandbox down"))
    client = HttpEnvironmentsClient(http, "http://env.test")

    readiness = await client.ready()

    assert readiness.ready is False
    assert http.last.audience == "environments-api"


async def test_file_and_command_calls_use_the_environments_audience() -> None:
    http = FakeHttp(
        Answer(body={"status": "ready", "sandbox_tier": "container", "keyring": {"status": "ok"}}),
        Answer(body={"id": "env-1", "name": "lucy-ses"}),
        Answer(body={"environments": [{"id": "env-1", "name": "lucy-ses"}]}),
        Answer(status_code=204),
        Answer(body={"path": "sessions/s1"}),
        Answer(
            body={
                "path": ".",
                "entries": [{"name": "notes.md", "path": "notes.md", "kind": "file"}],
            }
        ),
        Answer(
            body={
                "path": "notes.md",
                "content": "ab",
                "size": 4,
                "offset": 0,
                "truncated": True,
            }
        ),
        Answer(body={"path": "notes.md", "size": 2}),
        Answer(
            body={
                "matches": [{"path": "notes.md", "lines": [{"line": 1, "text": "ab"}]}],
                "total_matches": 1,
                "truncated": False,
                "skipped_binary": ["bin"],
                "skipped_large": [],
                "skipped_unavailable": [],
            }
        ),
        Answer(
            body={
                "path": "notes.md",
                "size": 3,
                "etag": "e1",
                "diff": "-a\n+b",
                "applied_hunks": [1],
            }
        ),
        Answer(body={"path": "notes.md", "size": 3, "applied_hunks": [1]}),
        Answer(status_code=204),
        Answer(body={"path": "renamed.md", "size": 3}),
        Answer(body={"command": "ls", "exit_code": 0, "output": "", "state": "idle"}),
    )
    client = HttpEnvironmentsClient(http, "http://env.test")

    assert (await client.ready()).ready is True
    created = await client.create("lucy-ses", profile="work")
    listed = await client.environments(profile="work")
    await client.destroy("env-1")
    folder = await client.mkdir("env-1", "sessions/s1")
    listing = await client.files("env-1")
    read = await client.read("env-1", "notes.md")
    written = await client.write("env-1", "notes.md", "hi")
    found = await client.search("env-1", ".", "ab")
    edited = await client.edit("env-1", "notes.md", "a", "b")
    patched = await client.patch("env-1", "notes.md", "+++")
    await client.delete("env-1", "notes.md", recursive=True)
    moved = await client.move("env-1", "notes.md", "renamed.md")
    ran = await client.run("env-1", "ls")

    assert created.name == "lucy-ses"
    assert listed[0].environment_id == "env-1"
    assert folder == "sessions/s1"
    assert listing.entries[0].name == "notes.md"
    assert read.notice == "showing 2 of 4 bytes"
    assert written.size == 2
    assert found.skipped_binary == ("bin",)
    assert edited.diff
    assert patched.applied_hunks == (1,)
    assert moved.path == "renamed.md"
    assert ran.exit_code == 0
    assert all(call.audience == "environments-api" for call in http.calls)
    assert http.calls[1].headers == {"X-Keyring-Profile": "work"}


async def test_the_in_memory_workspace_keeps_two_environments_apart() -> None:
    fake = FakeEnvironmentsClient()
    first = await fake.create("one", profile="personal")
    second = await fake.create("two", profile="work")
    await fake.write(first.environment_id, "a.md", "one")
    await fake.write(second.environment_id, "a.md", "two")
    await fake.mkdir(first.environment_id, "sub")

    owned = await fake.environments(profile="personal")
    read = await fake.read(first.environment_id, "a.md", max_bytes=1)
    listed = await fake.files(first.environment_id)
    found = await fake.search(first.environment_id, ".", "on")
    edited = await fake.edit(first.environment_id, "a.md", "o", "O")
    await fake.patch(first.environment_id, "b.md", "patched")
    moved = await fake.move(first.environment_id, "b.md", "c.md")
    fake.script("pwd", Ran(command="pwd", exit_code=0, output="/ws", state="idle"))
    ran = await fake.run(first.environment_id, "pwd")
    await fake.delete(first.environment_id, "a.md")
    await fake.destroy(second.environment_id)

    assert [item.name for item in owned] == ["one"]
    assert read.truncated is True
    assert listed.entries
    assert found.total_matches >= 1
    assert edited.path == "a.md"
    assert moved.path == "c.md"
    assert ran.output == "/ws"
    assert second.environment_id not in fake.workspaces
    with pytest.raises(KeyError):
        await fake.mkdir("missing", "x")
    assert (await fake.ready()).ready is True
