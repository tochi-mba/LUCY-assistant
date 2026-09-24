"""Resume orientation reads the session journal without failing the turn on git."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lucy_api.clients.environments import ARCHIVED, Environment, FakeEnvironmentsClient, Ran
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


async def test_workspace_live_exposes_how_long_the_sandbox_has_left() -> None:
    fake = FakeEnvironmentsClient()
    activity = datetime.now(UTC) - timedelta(hours=1)
    fake.seed(Environment("env-1", "Conversation", last_activity_at=activity))
    live = WorkspaceLive(fake, WorkspaceScope("env-1", "sess-a"), retention_hours=24)
    snapshot = await live.fetch("sess-a")
    assert snapshot is not None
    assert snapshot.expires_in_seconds is not None
    assert 22 * 3600 < snapshot.expires_in_seconds <= 23 * 3600

    fake.seed(Environment("env-1", "Conversation", last_activity_at=activity.replace(tzinfo=None)))
    naive = await live.fetch("sess-a")
    assert naive is not None
    assert naive.expires_in_seconds is not None
    assert 22 * 3600 < naive.expires_in_seconds <= 23 * 3600

    fake.seed(Environment("env-1", "Conversation"))
    missing_stamp = await live.fetch("sess-a")
    assert missing_stamp is not None
    assert missing_stamp.expires_in_seconds is None

    stranger = FakeEnvironmentsClient()
    stranger.seed(Environment("other", "Conversation", last_activity_at=activity))
    missing_env = await WorkspaceLive(
        stranger, WorkspaceScope("env-1", "sess-a"), retention_hours=24
    ).fetch("sess-a")
    assert missing_env is not None
    assert missing_env.expires_in_seconds is None

    fake.seed(
        Environment("env-1", "Conversation", last_activity_at=datetime.now(UTC) - timedelta(days=2))
    )
    expired = await live.fetch("sess-a")
    assert expired is not None
    assert expired.expires_in_seconds == 0.0


async def test_a_workspace_listing_outage_omits_expiry_rather_than_the_group() -> None:
    class Dead(FakeEnvironmentsClient):
        async def environments(self, *, profile: str = "") -> tuple[Environment, ...]:
            del profile
            raise DownstreamError("environments", 503, "down")

    live = WorkspaceLive(Dead(), WorkspaceScope("env-1", "sess-a"), retention_hours=24)
    snapshot = await live.fetch("sess-a")
    assert snapshot is not None
    assert snapshot.path.endswith("sess-a")
    assert snapshot.expires_in_seconds is None


# --- the sandbox's clock, not the hub's --------------------------------------------------------
#
# Environments-api archives an environment, and wipes it, at its last activity plus its own
# stamped `environment_idle_ttl_seconds`, never while a shell is open, and lists it as
# `archived` afterwards (`app/environments/service.py`, `reap` and `_archive`).


def _live(environment: Environment) -> WorkspaceLive:
    fake = FakeEnvironmentsClient()
    fake.seed(environment)
    return WorkspaceLive(fake, WorkspaceScope("env-1", "sess-a"), retention_hours=24)


async def test_the_sandbox_idle_ttl_decides_the_expiry_rather_than_the_hub_setting() -> None:
    """The bug, named: with the sandbox archiving after two idle hours and the hub setting at
    twenty-four, the live block said "sandbox expires in 22h" about a workspace an hour
    from being wiped."""
    activity = datetime.now(UTC) - timedelta(hours=1)
    snapshot = await _live(
        Environment("env-1", "Conversation", last_activity_at=activity, idle_ttl_seconds=7_200.0)
    ).fetch("sess-a")

    assert snapshot is not None
    assert snapshot.expires_in_seconds is not None
    assert 0 < snapshot.expires_in_seconds <= 3600


async def test_an_archived_workspace_has_expired_whatever_its_last_activity_says() -> None:
    """Archiving does not touch `last_activity_at`, so the clock alone kept counting down
    after the sandbox had already wiped everything."""
    activity = datetime.now(UTC) - timedelta(minutes=1)
    snapshot = await _live(
        Environment("env-1", "Conversation", state=ARCHIVED, last_activity_at=activity)
    ).fetch("sess-a")

    assert snapshot is not None
    assert snapshot.expires_in_seconds == 0.0


async def test_a_workspace_with_a_shell_open_shows_no_countdown() -> None:
    """The reaper never archives an environment with a live shell, so no deadline is running."""
    activity = datetime.now(UTC) - timedelta(days=2)
    snapshot = await _live(
        Environment("env-1", "Conversation", shells_running=1, last_activity_at=activity)
    ).fetch("sess-a")

    assert snapshot is not None
    assert snapshot.expires_in_seconds is None
