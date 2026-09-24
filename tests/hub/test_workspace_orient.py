"""The workspace group's source: where the work is every turn, and what is in it on a resume.

Read in the requests the hub actually sent: "ready" about a workspace the sandbox had archived,
the first commit as the "last checkpoint" and again in the log, `smoke git is available`, and on
a resume the journal's seeded header and `{"tasks":[]}` passed through as if they were news --
and, once the journal grew, its oldest entries rather than its latest.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from lucy_api.clients.environments import ARCHIVED, Environment, FakeEnvironmentsClient, Ran
from lucy_api.clients.errors import DownstreamError
from lucy_api.context.build import Live
from lucy_api.context.sources import Sources
from lucy_api.context.state import JOURNAL_FILE, WORK_FILE
from lucy_api.context.types import WorkspaceSnapshot
from lucy_api.sessions.scope import PROGRESS_FILE, PROGRESS_STARTER, TASKS_FILE, WorkspaceScope
from lucy_api.turn.supervisor import _arm_workspace
from lucy_api.workspace.orient import (
    GIT_LOG,
    GIT_STATUS,
    JOURNAL_CHARS,
    TASKS_BYTES,
    WorkspaceLive,
    _clip,
    _status_paths,
    orient,
)

JOURNAL = f"sessions/sess-a/{PROGRESS_FILE}"
TASKS = f"sessions/sess-a/{TASKS_FILE}"
CONVERSATION = Environment("env-1", "Conversation", profile="personal")


def _workspace(
    *,
    journal: str = PROGRESS_STARTER + "Did the dates.\n",
    environment: Environment = CONVERSATION,
) -> tuple[FakeEnvironmentsClient, WorkspaceScope]:
    fake = FakeEnvironmentsClient()
    fake.seed(
        environment,
        files=((JOURNAL, journal), (TASKS, '{"tasks":[{"id":"1","title":"dates"}]}\n')),
    )
    return fake, WorkspaceScope("env-1", "sess-a")


async def _resumed(fake: FakeEnvironmentsClient, workspace: WorkspaceScope) -> WorkspaceSnapshot:
    source = WorkspaceLive(fake, workspace)
    source.arm(True)
    snapshot = await source.fetch("sess-a")
    assert snapshot is not None
    return snapshot


# --- every assemble ----------------------------------------------------------------------------


async def test_every_turn_says_where_and_whether_it_is_usable_without_reading_files() -> None:
    fake, workspace = _workspace()
    snapshot = await WorkspaceLive(fake, workspace, profile="personal").fetch("sess-a")

    assert snapshot is not None
    assert (snapshot.path, snapshot.ready) == (workspace.root, True)
    assert snapshot.journal == ""
    assert fake.ran == []


async def test_a_workspace_the_sandbox_does_not_list_is_not_ready() -> None:
    fake = FakeEnvironmentsClient()
    fake.seed(Environment("other", "Conversation"))
    source = WorkspaceLive(fake, WorkspaceScope("env-1", "sess-a"))
    source.arm(True)
    snapshot = await source.fetch("sess-a")

    assert snapshot is not None
    assert snapshot.ready is False
    assert snapshot.expires_in_seconds is None
    assert fake.ran == []


async def test_a_listing_outage_says_not_ready_rather_than_guessing() -> None:
    class Dead(FakeEnvironmentsClient):
        async def environments(self, *, profile: str = "") -> tuple[Environment, ...]:
            del profile
            raise DownstreamError("environments", 503, "down")

    snapshot = await WorkspaceLive(Dead(), WorkspaceScope("env-1", "sess-a")).fetch("sess-a")
    assert snapshot is not None
    assert snapshot.path.endswith("sess-a")
    assert (snapshot.ready, snapshot.expires_in_seconds) == (False, None)


async def test_an_archived_workspace_is_not_ready_and_is_not_read() -> None:
    """It was "ready - sandbox has expired" in one breath, and a resume read files that the
    sandbox refuses to serve from an archived workspace."""
    fake, workspace = _workspace(environment=replace(CONVERSATION, state=ARCHIVED))
    snapshot = await _resumed(fake, workspace)

    assert snapshot.ready is False
    assert snapshot.expires_in_seconds == 0.0
    assert snapshot.journal == ""
    assert fake.ran == []


# --- a resume ----------------------------------------------------------------------------------


async def test_a_resume_reads_the_journal_the_tasks_and_git_once_each() -> None:
    fake, workspace = _workspace()
    fake.script(GIT_LOG, Ran(command=GIT_LOG, exit_code=0, output="abc123 first\ndef456 second\n"))
    fake.script(GIT_STATUS, Ran(command=GIT_STATUS, exit_code=0, output=" M dates.txt\n"))
    snapshot = await _resumed(fake, workspace)

    assert snapshot.journal == "Did the dates."
    assert snapshot.tasks == "1 task: dates"
    assert snapshot.commits == ("abc123 first", "def456 second")
    assert snapshot.changed_files == ("dates.txt",)
    assert snapshot.git_missing is False


async def test_a_journal_holding_only_its_seeded_header_says_nothing() -> None:
    fake, workspace = _workspace(journal=PROGRESS_STARTER)
    assert (await _resumed(fake, workspace)).journal == ""


async def test_a_long_journal_shows_its_latest_entries_not_its_first() -> None:
    """The bug, named: the head was read, so a long journal showed the oldest entries."""
    entries = "".join(f"- entry {index}\n" for index in range(300))
    fake, workspace = _workspace(journal=PROGRESS_STARTER + entries)
    journal = (await _resumed(fake, workspace)).journal

    assert journal.startswith("…")
    assert journal.endswith("- entry 299")
    assert "entry 0 " not in journal
    assert len(journal) <= JOURNAL_CHARS + 1


async def test_a_binary_journal_is_not_shown() -> None:
    fake, workspace = _workspace(journal="\0\1\2")
    assert (await _resumed(fake, workspace)).journal == ""


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ('{"tasks":[]}', ""),
        (
            '{"tasks":[{"title":"dates","status":"done"},{"name":"venue"},{"id":"3"},{}]}',
            "4 tasks: dates (done); venue; 3; untitled",
        ),
        ('["book it", "pay"]', "2 tasks: book it; pay"),
        ("{nope", "not valid JSON"),
        ('{"tasks": {"one": 1}}', "not a list of tasks"),
    ],
)
async def test_tasks_read_as_a_line_a_person_would_write(body: str, said: str) -> None:
    fake, workspace = _workspace()
    fake.contents[("env-1", TASKS)] = body
    assert (await _resumed(fake, workspace)).tasks == said


async def test_a_tasks_file_too_large_to_read_whole_is_said_to_be() -> None:
    """A window of JSON is not JSON: it is summarised as a size, not shown as a fragment."""
    fake, workspace = _workspace()
    fake.contents[("env-1", TASKS)] = '{"tasks":["' + "x" * TASKS_BYTES + '"]}'
    assert (await _resumed(fake, workspace)).tasks.endswith("too large to summarise here")


async def test_a_git_outage_still_returns_the_journal_and_says_git_is_missing() -> None:
    class NoGit(FakeEnvironmentsClient):
        async def run(
            self,
            environment_id: str,
            command: str,
            *,
            cwd: str = ".",
            timeout_ms: int = 0,
            max_output_bytes: int = 0,
            tail: bool = False,
        ) -> Ran:
            del environment_id, command, cwd, timeout_ms, max_output_bytes, tail
            raise DownstreamError("environments", 503, "git missing")

        async def read(
            self,
            environment_id: str,
            path: str,
            *,
            offset: int = 0,
            max_bytes: int | None = None,
        ) -> object:
            if path.endswith(TASKS_FILE):
                raise KeyError(path)
            return await super().read(environment_id, path, offset=offset, max_bytes=max_bytes)

    fake = NoGit()
    fake.seed(Environment("env-1", "Conversation"), files=((JOURNAL, PROGRESS_STARTER + "kept\n"),))
    snapshot = await orient(fake, WorkspaceScope("env-1", "sess-a"))

    assert snapshot.journal == "kept"
    assert snapshot.tasks == ""
    assert snapshot.git_missing is True
    assert snapshot.commits == ()


async def test_a_journal_the_sandbox_will_not_serve_is_left_out() -> None:
    class Gone(FakeEnvironmentsClient):
        async def read(
            self,
            environment_id: str,
            path: str,
            *,
            offset: int = 0,
            max_bytes: int | None = None,
        ) -> object:
            del environment_id, path, offset, max_bytes
            raise DownstreamError("environments", 404, "gone")

    fake = Gone()
    fake.seed(Environment("env-1", "Conversation"))
    snapshot = await orient(fake, WorkspaceScope("env-1", "sess-a"))
    assert (snapshot.journal, snapshot.tasks) == ("", "")


async def test_a_failed_git_command_is_treated_as_missing() -> None:
    fake, workspace = _workspace()
    fake.script(GIT_LOG, Ran(command=GIT_LOG, exit_code=128, output=""))
    fake.script(GIT_STATUS, Ran(command=GIT_STATUS, exit_code=128, output="fatal"))
    snapshot = await orient(fake, workspace)

    assert snapshot.git_missing is True
    assert snapshot.changed_files == ()
    assert snapshot.commits == ()


# --- arming ------------------------------------------------------------------------------------


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
    assert first.journal == "Did the dates."
    _arm_workspace(Live(sources=Sources(workspace=source)), resume=False)
    fake.ran.clear()
    second = await source.fetch("sess-a")
    assert second is not None
    assert second.journal == ""
    assert fake.ran == []


def test_status_paths_ignore_blank_and_short_lines() -> None:
    assert _status_paths(" M a.py\n\n?? b.py\nx") == ("a.py", "b.py")
    assert _status_paths(" M  \n??  \n M \n") == ()
    assert _clip("word " * 200, 20).endswith("…")


def test_the_live_block_names_the_files_the_session_owns() -> None:
    """The context layer does not import sessions, so it names these itself."""
    assert (JOURNAL_FILE, WORK_FILE) == (PROGRESS_FILE, TASKS_FILE)


# --- the sandbox's clock, not the hub's --------------------------------------------------------
#
# Environments-api archives an environment, and wipes it, at its last activity plus its own
# stamped `environment_idle_ttl_seconds`, never while a shell is open, and lists it as
# `archived` afterwards (`app/environments/service.py`, `reap` and `_archive`).


def _live(environment: Environment) -> WorkspaceLive:
    fake = FakeEnvironmentsClient()
    fake.seed(environment)
    return WorkspaceLive(fake, WorkspaceScope("env-1", "sess-a"), retention_hours=24)


async def test_workspace_live_exposes_how_long_the_sandbox_has_left() -> None:
    activity = datetime.now(UTC) - timedelta(hours=1)
    for stamp in (activity, activity.replace(tzinfo=None)):
        snapshot = await _live(Environment("env-1", "Conversation", last_activity_at=stamp)).fetch(
            "sess-a"
        )
        assert snapshot is not None
        assert snapshot.expires_in_seconds is not None
        assert 22 * 3600 < snapshot.expires_in_seconds <= 23 * 3600

    unstamped = await _live(Environment("env-1", "Conversation")).fetch("sess-a")
    assert unstamped is not None
    assert unstamped.expires_in_seconds is None

    long_idle = datetime.now(UTC) - timedelta(days=2)
    expired = await _live(Environment("env-1", "Conversation", last_activity_at=long_idle)).fetch(
        "sess-a"
    )
    assert expired is not None
    assert expired.expires_in_seconds == 0.0


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
