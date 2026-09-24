"""Sibling-owned facts become small, provenance-preserving prompt feeds."""

from __future__ import annotations

from datetime import UTC, datetime

from lucy_api.clients.environments import Environment, FakeEnvironmentsClient, Ran
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.live_feeds import (
    GIT_BRANCH,
    MusicFeeds,
    PersonaFeeds,
    ResearchFeeds,
    UserFeeds,
    WorkspaceFeeds,
)
from lucy_api.clients.spotify import Device, FakeSpotifyClient, NowPlaying, Track
from lucy_api.clients.testing import Answer, FakeHttp, problem
from lucy_api.clients.user import HttpUserClient
from lucy_api.context.feeds import FeedRequest, Volatility
from lucy_api.context.types import Trust

REQUEST = FeedRequest(profile="personal", session_id="ses_1")


async def test_persona_feed_keeps_each_pin_and_its_provenance() -> None:
    http = FakeHttp(
        Answer(
            body={
                "persona": {
                    "display_name": "Ada",
                    "pronouns": "they/them",
                    "summary": "Dry and concise.",
                    "updated_at": "2026-09-16T10:00:00Z",
                },
                "fields": [
                    {
                        "key": "forms_of_address",
                        "value": ["Alex"],
                        "source": "owner",
                        "asserted_by": "persona",
                        "updated_at": "2026-09-16T11:00:00Z",
                    }
                ],
                "notes": [
                    {
                        "note_id": "note_1",
                        "body": "Ask before broad refactors.",
                        "source": "assistant",
                        "asserted_by": "persona",
                        "updated_at": "2026-09-16T12:00:00Z",
                    },
                    {
                        "note_id": "note_2",
                        "body": "Prefers tea.",
                        "source": "mystery",
                        "asserted_by": "persona",
                    },
                ],
            }
        )
    )

    feeds = await PersonaFeeds(http, "http://persona").fetch(REQUEST)

    assert feeds[0].volatility is Volatility.standing
    assert [entry.setting_key for entry in feeds[0].entries] == [
        "identity",
        "identity",
        "notes",
        "notes",
    ]
    assert feeds[0].entries[1].trust is Trust.stated
    assert feeds[0].entries[2].trust is Trust.inferred
    assert feeds[0].entries[3].trust is Trust.untrusted
    assert feeds[0].entries[2].recorded_at == datetime(2026, 9, 16, 12, tzinfo=UTC)
    # The audience persona pins, which is not its service name. Asking for `persona-api` put
    # `token_rejected reason=audience` in persona's log on every turn ever served, and left
    # the persona out of every prompt.
    assert http.last.audience == "persona"
    assert http.last.headers == {"X-Keyring-Profile": "personal"}


async def test_missing_persona_is_normal_absence_not_live_state_trouble() -> None:
    http = FakeHttp(problem(404, code="not-found"))
    assert await PersonaFeeds(http, "http://persona").fetch(REQUEST) == ()


async def test_long_persona_values_name_the_omission_and_keep_a_reference() -> None:
    http = FakeHttp(
        Answer(
            body={
                "persona": {"updated_at": "2026-09-16T10:00:00Z"},
                "fields": [],
                "notes": [
                    {
                        "note_id": "note_long",
                        "body": "x" * 500,
                        "source": "owner",
                        "asserted_by": "persona",
                    }
                ],
            }
        )
    )
    line = (await PersonaFeeds(http, "http://persona").fetch(REQUEST))[0].entries[0].line
    assert len(line) <= 240
    assert "showing" in line
    assert "of 500 characters" in line
    assert "ref note_long" in line


async def test_music_feed_reports_only_loaded_track_and_active_device() -> None:
    client = FakeSpotifyClient()
    client.state = NowPlaying(
        track=Track(
            name="Prelude", artists=("Debussy",), uri="spotify:track:1", duration_ms=180_000
        ),
        progress_ms=61_000,
        is_playing=True,
    )
    client.seed(
        devices=(
            Device("d1", "Kitchen", "Speaker"),
            Device("d2", "Desk", "Computer", is_active=True),
        )
    )

    feed = (await MusicFeeds(client).fetch(REQUEST))[0]

    assert feed.volatility is Volatility.live
    assert feed.lines == (
        "playing: Prelude by Debussy; 1:01 of 3:00 [ref spotify:track:1]",
        "active device: Desk (Computer) [ref d2]",
    )
    assert client.asked == ["personal", "personal"]


async def test_music_feed_publishes_shuffle_and_repeat_when_the_player_reports_them() -> None:
    client = FakeSpotifyClient()
    client.state = NowPlaying(
        track=Track(
            name="Prelude", artists=("Debussy",), uri="spotify:track:1", duration_ms=60_000
        ),
        progress_ms=1_000,
        is_playing=True,
        shuffled=False,
        repeat="track",
    )
    feed = (await MusicFeeds(client).fetch(REQUEST))[0]
    assert "queue is in order" in feed.lines
    assert "repeat: track" in feed.lines


async def test_empty_player_and_no_active_device_publish_nothing() -> None:
    client = FakeSpotifyClient()
    client.seed(devices=(Device("d1", "Kitchen"),))
    assert await MusicFeeds(client).fetch(REQUEST) == ()


async def test_workspace_feed_selects_only_the_attached_environment() -> None:
    client = FakeEnvironmentsClient()
    client.seed(Environment("other", "Other", profile="personal", shells_running=9))
    client.seed(
        Environment(
            "wanted",
            "Project",
            profile="personal",
            state="ready",
            sandbox_tier="container",
            shells_running=2,
        )
    )

    feed = (await WorkspaceFeeds(client, "wanted").fetch(REQUEST))[0]

    assert feed.lines == (
        "2 shells running",
        "sandbox isolation: container",
    )
    assert await WorkspaceFeeds(client, "missing").fetch(REQUEST) == ()


async def test_workspace_feed_adds_the_git_branch_but_never_the_path_again() -> None:
    client = FakeEnvironmentsClient()
    client.seed(Environment("wanted", "Project", profile="personal", state="ready"))
    client.script(GIT_BRANCH, Ran(command=GIT_BRANCH, exit_code=0, output="main\n"))
    feed = (await WorkspaceFeeds(client, "wanted", workspace_rel="sess-a").fetch(REQUEST))[0]
    assert all("sess-a" not in line for line in feed.lines), "the workspace group says it"
    assert "git branch: main" in feed.lines

    client.script(GIT_BRANCH, Ran(command=GIT_BRANCH, exit_code=0, output="HEAD\n"))
    detached = (await WorkspaceFeeds(client, "wanted", workspace_rel="sess-a").fetch(REQUEST))[0]
    assert all("git branch" not in line for line in detached.lines)

    client.script(GIT_BRANCH, Ran(command=GIT_BRANCH, exit_code=128, output="fatal"))
    failed = (await WorkspaceFeeds(client, "wanted", workspace_rel="sess-a").fetch(REQUEST))[0]
    assert all("git branch" not in line for line in failed.lines)


async def test_a_git_outage_omits_the_branch_rather_than_the_whole_workspace_feed() -> None:
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

    client = NoGit()
    client.seed(Environment("wanted", "Project", profile="personal", state="ready"))
    feed = (await WorkspaceFeeds(client, "wanted", workspace_rel="sess-a").fetch(REQUEST))[0]
    assert feed.lines == ("0 shells running", "sandbox isolation: unknown")
    assert all("git branch" not in line for line in feed.lines)


async def test_research_feed_names_the_backend_as_a_product_word() -> None:
    google = (await ResearchFeeds("google").fetch(REQUEST))[0]
    assert google.lines == ("search backend: Google",)
    searx = (await ResearchFeeds("searxng").fetch(REQUEST))[0]
    assert searx.lines == ("search backend: SearXNG",)
    assert await ResearchFeeds("unknown").fetch(REQUEST) == ()
    assert await ResearchFeeds("").fetch(REQUEST) == ()


async def test_a_pin_without_an_id_still_has_a_stable_feed_key() -> None:
    http = FakeHttp(
        Answer(
            body={
                "entries": [
                    {"entry_type": "field", "key": "tz", "value": "UTC", "pinned": True},
                ]
            }
        )
    )
    feed = (await UserFeeds(HttpUserClient(http, "http://account")).fetch(REQUEST))[0]
    assert feed.entries[0].key == "pinned_1"
    assert feed.version == ""


async def test_account_feed_keeps_each_pin_and_its_provenance() -> None:
    http = FakeHttp(
        Answer(
            body={
                "entries": [
                    {
                        "entry_id": "ent_1",
                        "entry_type": "field",
                        "key": "preferred_name",
                        "value": "Ada",
                        "source": "stated",
                        "asserted_by": "user",
                        "pinned": True,
                        "updated_at": "2026-09-16T10:00:00Z",
                    },
                    {
                        "entry_id": "ent_2",
                        "entry_type": "note",
                        "body": "Prefers tea.",
                        "source": "imported",
                        "asserted_by": "user",
                        "pinned": True,
                        "updated_at": "2026-09-16T12:00:00Z",
                    },
                    {
                        "entry_id": "ent_3",
                        "entry_type": "field",
                        "key": "nickname",
                        "value": "A",
                        "source": "observed",
                        "asserted_by": "user",
                        "pinned": True,
                    },
                ]
            }
        )
    )

    feeds = await UserFeeds(HttpUserClient(http, "http://account")).fetch(REQUEST)

    assert feeds[0].id == "account"
    assert feeds[0].volatility is Volatility.standing
    assert feeds[0].version == "2026-09-16T12:00:00+00:00"
    assert [entry.setting_key for entry in feeds[0].entries] == ["pinned", "pinned", "pinned"]
    assert feeds[0].entries[0].trust is Trust.stated
    assert feeds[0].entries[1].trust is Trust.untrusted
    assert feeds[0].entries[2].trust is Trust.observed
    assert feeds[0].entries[0].recorded_at == datetime(2026, 9, 16, 10, tzinfo=UTC)
    assert http.last.audience == "user"


async def test_missing_or_empty_account_pins_are_normal_absence() -> None:
    missing = FakeHttp(problem(404, code="not-found"))
    empty = FakeHttp(Answer(body={"entries": []}))
    assert await UserFeeds(HttpUserClient(missing, "http://account")).fetch(REQUEST) == ()
    assert await UserFeeds(HttpUserClient(empty, "http://account")).fetch(REQUEST) == ()
