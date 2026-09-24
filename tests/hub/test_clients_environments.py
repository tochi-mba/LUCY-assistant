"""The workspace client drops the host path and never invents an exit code."""

from __future__ import annotations

import pytest

from lucy_api.clients.environments import (
    ARCHIVED,
    ARCHIVED_CODE,
    EXEC_MARGIN_SECONDS,
    Environment,
    FakeEnvironmentsClient,
    HttpEnvironmentsClient,
    Ran,
    _environment,
    _exit_code,
)
from lucy_api.clients.errors import ConflictError
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
                "next_offset": 2,
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
    assert read.notice == "showing bytes 0-2 of 4; continue with offset=2"
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


# --- waiting longer than the work you asked for ------------------------------------------------
#
# The sandbox opens a shell, runs the command and closes it, and charges for all three: a
# `git rev-parse` asked for with a sixty-second ceiling took 15.1 seconds of wall clock, every
# time. The client allowed itself the default ten, so every workspace command ever run timed
# out on this side while the sandbox was still working on the other.


async def test_exec_waits_longer_than_the_command_it_asked_for() -> None:
    """You cannot ask for thirty seconds of work and wait ten."""
    http = FakeHttp(Answer(body={"command": "echo hi", "exit_code": 0, "output": "hi"}))
    await HttpEnvironmentsClient(http, "http://environments.test").run(
        "env_1", "echo hi", timeout_ms=30_000
    )
    assert http.last.timeout_seconds == 30 + EXEC_MARGIN_SECONDS
    assert http.last.timeout_seconds > 30


async def test_an_ordinary_call_leaves_the_timeout_to_the_client() -> None:
    """`None` means the client's own figure, which is what almost every call wants: a sibling
    that is either up or down does not need longer."""
    http = FakeHttp(Answer(body={"data": []}))
    await HttpEnvironmentsClient(http, "http://environments.test").environments()
    assert http.last.timeout_seconds is None


# --- the only way back from archived ---------------------------------------------------------
#
# Environments-api archives an environment that sat idle past its time to live, wipes it, and
# keeps listing it. Every file and shell call is then a 409 until it is reset.


def _environment_view() -> dict[str, object]:
    """One environment as `POST /v1/environments/{id}/reset` answers, field for field.

    `Environments-api/app/environments/service.py` `environment_view` dumps the whole
    `EnvironmentRecord` (`app/environments/models.py`) and adds `shells_running` and
    `disk_bytes`. `state` is `EnvironmentState` from `app/constants.py`.
    """
    return {
        "id": "env-1",
        "account_id": "acct_example",
        "profile": "personal",
        "name": "lucy-3f2a",
        "labels": {},
        "credentials": [],
        "network": True,
        "limits": {
            "max_memory_bytes": None,
            "max_cpu_seconds": None,
            "max_file_size_bytes": None,
            "max_processes_per_shell": None,
        },
        "state": "active",
        "sandbox_tier": "container",
        "created_at": 1_789_000_000.0,
        "updated_at": 1_789_090_000.0,
        "last_activity_at": 1_789_090_000.0,
        "archived_at": None,
        "shells": [],
        "environment_idle_ttl_seconds": 86_400.0,
        "shell_idle_ttl_seconds": 1_800.0,
        "shells_running": 0,
        "disk_bytes": 0,
    }


async def test_a_reset_posts_to_the_environment_and_reads_back_its_new_state() -> None:
    """`Environments-api/app/api/routes/environments.py` `reset_environment`:
    `POST /v1/environments/{environment_id}/reset`, answering the environment view."""
    http = FakeHttp(Answer(body=_environment_view()))

    environment = await HttpEnvironmentsClient(http, "http://env.test").reset("env/1")

    assert http.last.method == "POST"
    assert http.last.url == "http://env.test/v1/environments/env%2F1/reset"
    assert http.last.audience == "environments-api"
    assert environment.environment_id == "env-1"
    assert environment.state == "active"


async def test_the_in_memory_workspace_refuses_an_archived_environment_until_it_is_reset() -> None:
    """The fake archives as the reaper does and refuses as the sandbox does, so a hub test
    that forgets to reset fails the way production did."""
    fake = FakeEnvironmentsClient()
    fake.seed(Environment("env-1", "lucy-3f2a"), files=(("sessions/s1/notes.md", "kept"),))
    fake.archive("env-1")

    assert fake.workspaces["env-1"].state == ARCHIVED
    assert fake.contents == {}
    with pytest.raises(ConflictError) as refused:
        await fake.mkdir("env-1", "sessions/s1")
    assert refused.value.status == 409
    assert refused.value.code == ARCHIVED_CODE

    revived = await fake.reset("env-1")

    assert revived.state == "active"
    assert fake.resets == ["env-1"]
    assert await fake.mkdir("env-1", "sessions/s1") == "sessions/s1"


async def test_the_sandbox_refusal_of_an_archived_environment_reads_as_the_archived_code() -> None:
    """The body `EnvironmentArchivedError.to_problem` sends (`Environments-api/app/errors.py`),
    as it arrives: the hub resets on this code and on no other 409."""
    http = FakeHttp(
        Answer(
            status_code=409,
            body={
                "type": "urn:environments-api:error:environment_archived",
                "title": "Environment is archived",
                "status": 409,
                "detail": "environment env-1 is archived; reset it first",
                "code": "environment_archived",
            },
        )
    )

    with pytest.raises(ConflictError) as refused:
        await HttpEnvironmentsClient(http, "http://env.test").mkdir("env-1", "sessions/s1")

    assert refused.value.code == ARCHIVED_CODE


# --- whose clock the expiry runs on ----------------------------------------------------------


def test_the_stamped_idle_ttl_is_read_from_the_environment_view() -> None:
    """`environment_idle_ttl_seconds` is `EnvironmentRecord`'s own field name
    (`Environments-api/app/environments/models.py`), stamped at create from settings-api."""
    assert _environment(_environment_view()).idle_ttl_seconds == 86_400.0


def test_an_unstamped_or_malformed_idle_ttl_is_none_never_zero() -> None:
    """A null TTL means the sandbox's deployment default applies. Zero would mean expired."""
    view = _environment_view()
    for unusable in (None, True, "86400"):
        parsed = _environment({**view, "environment_idle_ttl_seconds": unusable})
        assert parsed.idle_ttl_seconds is None
    whole = _environment({**view, "environment_idle_ttl_seconds": 7_200})
    assert whole.idle_ttl_seconds == 7_200.0
