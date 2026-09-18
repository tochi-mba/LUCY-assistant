"""Resume orientation reads the session journal without failing the turn on git."""

from __future__ import annotations

from lucy_api.clients.environments import Environment, FakeEnvironmentsClient, Ran
from lucy_api.clients.errors import DownstreamError
from lucy_api.context.build import Live
from lucy_api.context.sources import Sources
from lucy_api.sessions.scope import PROGRESS_FILE, TASKS_FILE, WorkspaceScope
from lucy_api.turn.supervisor import _arm_workspace
from lucy_api.workspace.orient import (
    GIT_LOG,
    GIT_STATUS,
    WorkspaceLive,
    _clip,
    _status_paths,
    orient,
)


def _workspace() -> tuple[FakeEnvironmentsClient, WorkspaceScope]:
    fake = FakeEnvironmentsClient()
    fake.seed(
        Environment("env-1", "Conversation", profile="personal"),
        files=(
            (f"sessions/sess-a/{PROGRESS_FILE}", "# Progress\n\nDid the dates.\n"),
            (f"sessions/sess-a/{TASKS_FILE}", '{"tasks":[{"id":"1","title":"dates"}]}\n'),
        ),
    )
    return fake, WorkspaceScope("env-1", "sess-a")


async def test_a_disarmed_source_returns_only_the_path() -> None:
    fake, workspace = _workspace()
    live = WorkspaceLive(fake, workspace)
    snapshot = await live.fetch("sess-a")

    assert snapshot is not None
    assert snapshot.path == workspace.root
    assert snapshot.journal == ""
    assert fake.ran == []


async def test_arming_a_resume_reads_the_journal_and_git() -> None:
    fake, workspace = _workspace()
    fake.script(GIT_LOG, Ran(command=GIT_LOG, exit_code=0, output="abc123 " + ("x" * 500)))
    fake.script(GIT_STATUS, Ran(command=GIT_STATUS, exit_code=0, output=" M dates.txt\n"))
    source = WorkspaceLive(fake, workspace)
    source.arm(True)
    snapshot = await source.fetch("sess-a")

    assert snapshot is not None
    assert "Did the dates" in snapshot.journal
    assert "dates" in snapshot.tasks
    assert snapshot.last_checkpoint.startswith("abc123")
    assert "characters omitted" in snapshot.git_log
    assert snapshot.changed_files == ("dates.txt",)
    assert snapshot.smoke == "git is available"
    assert snapshot.cwd == workspace.root


async def test_a_git_outage_still_returns_cwd_and_the_journal() -> None:
    class NoGit(FakeEnvironmentsClient):
        async def run(
            self,
            environment_id: str,
            command: str,
            *,
            cwd: str = ".",
            timeout_ms: int = 0,
            max_output_bytes: int = 0,
        ) -> Ran:
            del environment_id, command, cwd, timeout_ms, max_output_bytes
            raise DownstreamError("environments", 503, "git missing")

        async def read(
            self,
            environment_id: str,
            path: str,
            *,
            offset: int = 0,
            max_bytes: int | None = None,
        ) -> object:
            del offset, max_bytes
            if path.endswith("tasks.json"):
                raise KeyError(path)
            if path.endswith("progress.md"):
                raise DownstreamError("environments", 404, "gone")
            return await super().read(environment_id, path)

    fake = NoGit()
    fake.seed(
        Environment("env-1", "Conversation", profile="personal"),
        files=((f"sessions/sess-a/{PROGRESS_FILE}", "# Progress\nkept\n"),),
    )
    snapshot = await orient(fake, WorkspaceScope("env-1", "sess-a"))

    assert snapshot.cwd.endswith("sess-a")
    assert snapshot.journal == ""
    assert snapshot.tasks == ""
    assert snapshot.smoke == "git is not available in this sandbox"


async def test_a_failed_git_command_is_treated_as_missing() -> None:
    fake, workspace = _workspace()
    fake.script(GIT_LOG, Ran(command=GIT_LOG, exit_code=128, output=""))
    fake.script(GIT_STATUS, Ran(command=GIT_STATUS, exit_code=128, output="fatal"))
    snapshot = await orient(fake, workspace)

    assert snapshot.smoke == "git is not available in this sandbox"
    assert snapshot.changed_files == ()
    assert snapshot.git_log == ""


async def test_arm_workspace_is_a_no_op_without_a_hook() -> None:
    _arm_workspace(None, resume=True)
    _arm_workspace(Live(), resume=True)
    _arm_workspace(Live(sources=Sources()), resume=False)


async def test_arm_workspace_forwards_resume_to_the_source() -> None:
    fake, workspace = _workspace()
    source = WorkspaceLive(fake, workspace)
    _arm_workspace(Live(sources=Sources(workspace=source)), resume=True)
    first = await source.fetch("sess-a")
    assert first is not None
    assert "Did the dates" in first.journal
    _arm_workspace(Live(sources=Sources(workspace=source)), resume=False)
    fake.ran.clear()
    second = await source.fetch("sess-a")
    assert second is not None
    assert second.journal == ""
    assert fake.ran == []


async def test_a_long_journal_is_clipped_with_a_count() -> None:
    fake, workspace = _workspace()
    fake.contents[(workspace.environment_id, f"{workspace.root}/{PROGRESS_FILE}")] = "keep " * 200
    source = WorkspaceLive(fake, workspace)
    source.arm(True)
    snapshot = await source.fetch("sess-a")
    assert snapshot is not None
    assert "characters omitted" in snapshot.journal


def test_status_paths_ignore_blank_and_short_lines() -> None:
    assert _status_paths(" M a.py\n\n?? b.py\nx") == ("a.py", "b.py")
    assert _status_paths(" M  \n??  \n M \n") == ()
    clipped = _clip("word " * 200, 20)
    assert clipped.endswith("characters omitted]")
