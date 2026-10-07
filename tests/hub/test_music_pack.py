"""Music is gated by connection state and projected before the model sees it."""

import json
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.music import (
    CONFIRM_WAIT_SECONDS,
    Device,
    FakeMusicClient,
    NowPlaying,
    Play,
    Track,
)
from lucy_api.core.config import Settings
from lucy_api.mcp.skills import CATALOGUE
from lucy_api.packs.base import Availability, Bound, State
from lucy_api.packs.context import STEP_MARGIN_SECONDS
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.packs.music import (
    EITHER,
    NOT_A_REFERENCE,
    NOT_REACHED,
    NOT_TRIED,
    NOTHING_FOUND,
    QUEUE_WHAT,
    UNCONFIRMED_NOTE,
    UNREACHABLE,
    URI_PATTERN,
    MusicInputError,
    MusicPack,
    _named,
    _optional_int,
)
from lucy_api.packs.registry import limits_for
from lucy_api.packs.service import Capabilities, installed_packs
from lucy_api.prompt.docs import capability_doc, read_capability_doc
from lucy_api.sessions.scope import SessionScope


def setup(
    fake: FakeMusicClient, clock: Callable[[], float] | None = None
) -> tuple[Capabilities, object]:
    capabilities = Capabilities(
        (HelpPack(), MusicPack("http://music.test", client=fake, clock=clock))
    )
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a", profile="personal", session_id="ses_a", permission_mode="auto"
        )
    )
    # Pack tests exercise the operations themselves; the outward-action floor is pinned
    # in test_tools_invoke, not here.
    context.policy = replace(context.policy, confirm_outward_actions=False)
    return capabilities, context


async def test_unconnected_music_is_listed_but_has_no_model_tools() -> None:
    fake = FakeMusicClient()
    fake.is_connected = False
    capabilities, context = setup(fake)

    catalogue = await capabilities.probe(context)

    music = next(row for row in capabilities.listings(catalogue) if row["id"] == "music")
    assert music["state"] == "not_connected"
    assert music["offer_setup"] is True
    assert all(
        not tool["name"].startswith("music.")
        for tool in capabilities.tools(catalogue, "ses_a")["tools"]
    )


async def test_connecting_music_makes_its_operations_appear_on_the_next_probe() -> None:
    fake = FakeMusicClient()
    capabilities, context = setup(fake)

    catalogue = await capabilities.probe(context)
    names = {tool["name"] for tool in capabilities.tools(catalogue, "ses_a")["tools"]}

    assert {"music.find", "music.nowPlaying", "music.devices", "music.recent"} <= names
    assert {"music.play", "music.queue", "music.pause"} <= names


async def test_music_reads_and_writes_use_the_session_profile_and_small_projections() -> None:
    fake = FakeMusicClient()
    track = Track(
        name="Clair de lune",
        artists=("Claude Debussy",),
        album="Suite bergamasque",
        uri="spotify:track:1",
        duration_ms=300_000,
    )
    fake.stock("Clair de lune", track)
    fake.seed(
        devices=(Device("device-1", "Study", "speaker", True),),
        plays=(Play(track),),
    )
    fake.state = fake.state.__class__(track=track, progress_ms=90_000, is_playing=True)
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "find", "op": "music.find", "input": {"name": "Clair de lune"}},
                {"id": "now", "op": "music.nowPlaying", "input": {}},
                {"id": "devices", "op": "music.devices", "input": {}},
                {
                    "id": "play",
                    "op": "music.play",
                    "input": {"uri": "spotify:track:1", "device_id": "device-1"},
                },
            ]
        },
        context,
    )

    assert result["issues"] is None
    assert result["steps"][0]["items"][0]["artist"] == "Claude Debussy"
    assert result["steps"][1]["data"]["progress_ms"] == 90_000
    assert result["steps"][2]["data"]["devices"][0]["name"] == "Study"
    assert fake.played == [("personal", ("spotify:track:1",), "device-1")]


async def test_omitted_device_id_uses_the_person_s_default_speaker() -> None:
    fake = FakeMusicClient()
    capabilities, context = setup(fake)
    context.defaults["music.device_id"] = "kitchen"
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "play", "op": "music.play", "input": {"uri": "spotify:track:1"}},
            ]
        },
        context,
    )

    assert not result["issues"]
    assert fake.played == [("personal", ("spotify:track:1",), "kitchen")]


def test_music_declares_setup_and_write_permission() -> None:
    pack = MusicPack("http://music.test", client=FakeMusicClient())

    assert pack.docs == capability_doc("music")
    assert pack.setup() is not None
    assert pack.setup().steps[0].kind == "oauth"
    assert pack.permissions()[0].covers == ("music.play", "music.queue", "music.pause")


async def test_music_without_a_usable_token_is_unavailable() -> None:
    capabilities = Capabilities((HelpPack(), MusicPack("http://music.test")))
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a", profile="personal", session_id="ses_a", permission_mode="auto"
        )
    )
    catalogue = await capabilities.probe(context)
    music = next(row for row in capabilities.listings(catalogue) if row["id"] == "music")
    assert music["state"] == "unavailable"


async def test_music_probe_names_an_exchange_failure() -> None:
    class Boom:
        async def connected(self, profile: str) -> bool:
            del profile
            raise ExchangeError("no grant")

    capabilities, context = setup(Boom())  # type: ignore[arg-type]
    catalogue = await capabilities.probe(context)
    music = next(row for row in capabilities.listings(catalogue) if row["id"] == "music")
    assert music["state"] == "unavailable"
    assert "cannot act" in music["detail"]


async def test_music_probe_names_a_downstream_outage() -> None:
    class Boom:
        async def connected(self, profile: str) -> bool:
            del profile
            raise DownstreamError("music", 503)

    capabilities, context = setup(Boom())  # type: ignore[arg-type]
    catalogue = await capabilities.probe(context)
    music = next(row for row in capabilities.listings(catalogue) if row["id"] == "music")
    assert music["state"] == "unavailable"
    assert "could not be reached" in music["detail"]


async def test_music_probe_names_a_transport_outage() -> None:
    class Boom:
        async def connected(self, profile: str) -> bool:
            del profile
            raise TransportError("down", audience="music")

    capabilities, context = setup(Boom())  # type: ignore[arg-type]
    catalogue = await capabilities.probe(context)
    music = next(row for row in capabilities.listings(catalogue) if row["id"] == "music")
    assert music["state"] == "unavailable"
    assert "could not be reached" in music["detail"]


async def test_pause_and_queue_project_playback_the_same_way_play_does() -> None:
    fake = FakeMusicClient()
    track = Track(
        name="Clair de lune",
        artists=("Claude Debussy",),
        album="Suite bergamasque",
        uri="spotify:track:1",
        duration_ms=300_000,
    )
    fake.seed(plays=(Play(track),))
    fake.state = fake.state.__class__(track=track, progress_ms=10, is_playing=True)
    capabilities, context = setup(fake)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "queue", "op": "music.queue", "input": {"uri": "spotify:track:1"}},
                {"id": "pause", "op": "music.pause", "input": {}},
            ]
        },
        context,
    )
    assert result["issues"] is None


async def test_a_command_accepted_but_not_confirmed_is_an_answer_rather_than_a_failure() -> None:
    """The bug, named: a play the service had accepted but not yet seen take effect came back
    as a failed step, so the model told the person playback had failed while the track may
    already have been starting, and sent the command again."""
    fake = FakeMusicClient()
    track = Track(name="Reverie", artists=("Claude Debussy",), uri="spotify:track:0")
    fake.state = fake.state.__class__(track=track, progress_ms=64_000, is_playing=True)
    fake.confirms = False
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "play", "op": "music.play", "input": {"uri": "spotify:track:1"}},
                {"id": "queue", "op": "music.queue", "input": {"uri": "spotify:track:1"}},
                {"id": "pause", "op": "music.pause", "input": {}},
            ]
        },
        context,
    )

    assert result["issues"] is None
    for step in result["steps"]:
        answer = step["data"]
        assert answer["confirmed"] is False
        assert "note" not in answer, "the hub's advice is not part of the service's answer"
        assert UNCONFIRMED_NOTE in step["notices"]
        assert answer["track"]["name"] == "Reverie"
        assert answer["progress_ms"] == 64_000
    assert fake.played == [("personal", ("spotify:track:1",), "")]


async def test_a_search_that_could_not_run_is_an_outage_not_a_missing_song() -> None:
    """The bug, named: every lookup failed while the catalogue refused searches, the pack kept
    only items with a track, and Lucy told the person the song "didn't show up in the search
    -- do you remember the artist?"."""
    fake = FakeMusicClient()
    fake.lookup_down = "the catalogue could not be searched"
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "found", "op": "music.find", "input": {"name": "Joha"}}]}, context
    )

    step = result["steps"][0]
    assert step["status"] == "error"
    assert "music could not be reached just now" in step["error"]
    assert "do you remember" not in step["error"]


async def test_a_confirmed_command_carries_no_note() -> None:
    fake = FakeMusicClient()
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "pause", "op": "music.pause", "input": {}}]}, context
    )

    assert set(result["steps"][0]["data"]) == {"track", "progress_ms", "is_playing"}


def test_a_music_step_outlasts_the_wait_for_a_confirmed_command() -> None:
    """The bug, named: the client can wait as long as it likes, but the step around it gave up
    at ten seconds, so a play still being confirmed was reported to the model as a failure."""
    pack = MusicPack("http://music.test", client=FakeMusicClient())
    limits = limits_for((Bound(pack=pack, availability=Availability(state=State.ready)),))
    assert limits["stepTimeoutMs"] > CONFIRM_WAIT_SECONDS * 1_000


def test_a_boolean_year_is_not_treated_as_a_year() -> None:
    assert _optional_int(True) is None
    assert _optional_int(1999) == 1999


def test_the_music_audience_is_the_configured_service_s_own() -> None:
    """An audience names one service. Whatever answers the music contract is asked with a
    token minted for it, which is configuration and not a constant."""
    default = next(pack for pack in installed_packs() if pack.id == "music")
    other = next(
        pack for pack in installed_packs(music_audience="other-music") if pack.id == "music"
    )
    assert isinstance(default, MusicPack)
    assert isinstance(other, MusicPack)
    assert (default.audience, other.audience) == ("spotify-api", "other-music")
    assert Settings(_env_file=None).music_api_audience == "spotify-api"


# --- a found track, played and queued by reference ------------------------------------------

FOUND = Track(
    name="Clair de lune",
    artists=("Claude Debussy",),
    album="Suite bergamasque",
    uri="spotify:track:1",
    duration_ms=300_000,
)
FINDS = [
    {"id": "found", "op": "music.find", "input": {"name": "Clair de lune"}},
    {"id": "nothing", "op": "music.find", "input": {"name": "Nothing by that name"}},
]


async def test_play_and_queue_take_the_track_music_find_found_by_reference() -> None:
    """The bug, named: play took only a ``uri`` string, so the plan a model writes -- find,
    then play what was found -- sent the literal text ``$found`` to the music service."""
    fake = FakeMusicClient()
    fake.stock("Clair de lune", FOUND)
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                *FINDS,
                {"id": "play", "op": "music.play", "input": {"track": "$found"}},
                {"id": "queue", "op": "music.queue", "input": {"track": "$found[1]"}},
            ]
        },
        context,
    )

    assert result["issues"] is None
    assert [step["status"] for step in result["steps"]] == ["ok", "ok", "ok", "ok"]
    assert fake.played == [("personal", ("spotify:track:1",), "")]
    assert fake.queued == [("personal", "spotify:track:1", "")]


@pytest.mark.parametrize(
    ("operation", "given", "said"),
    [
        ("music.play", {"uri": "spotify:track:1", "track": "$found"}, EITHER),
        ("music.play", {"track": "$nothing"}, NOTHING_FOUND),
        ("music.queue", {}, QUEUE_WHAT),
    ],
)
async def test_a_track_named_in_a_way_the_operation_cannot_use_is_refused_with_the_fix(
    operation: str, given: dict[str, str], said: str
) -> None:
    fake = FakeMusicClient()
    fake.stock("Clair de lune", FOUND)
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [*FINDS, {"id": "act", "op": operation, "input": given}]}, context
    )

    step = result["steps"][-1]
    assert step["status"] == "error"
    assert step["error"].endswith(said)
    assert fake.played == []
    assert fake.queued == []


@pytest.mark.parametrize("uri", ["$found", " $found ", "$elsewhere", "\t$found[1]"])
async def test_a_reference_given_as_a_uri_is_refused_before_the_plan_runs(uri: str) -> None:
    """The bug, named: the `uri` check looked at the first character, so a reference with a
    space in front of it -- `" $found "` -- was not seen as one and went to the music
    service as the text written; and a bare `$found` was refused only once the step ran."""
    fake = FakeMusicClient()
    fake.stock("Clair de lune", FOUND)
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [*FINDS, {"id": "act", "op": "music.play", "input": {"uri": uri}}]}, context
    )

    assert result["steps"] == []
    assert [
        (issue["code"], issue["stepId"], list(issue["path"])) for issue in result["issues"]
    ] == [("step.invalid_input", "act", ["uri"])]
    assert result["issues"][0]["message"] == "input.uri: Invalid string."
    assert fake.played == []


async def test_a_uri_with_whitespace_around_it_is_the_uri_inside() -> None:
    fake = FakeMusicClient()
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "play", "op": "music.play", "input": {"uri": " spotify:track:1\n"}}]},
        context,
    )

    assert result["issues"] is None
    assert fake.played == [("personal", ("spotify:track:1",), "")]


async def test_the_uri_field_tells_the_model_where_a_reference_goes() -> None:
    fake = FakeMusicClient()
    capabilities, context = setup(fake)
    catalogue = await capabilities.probe(context)

    schema = json.dumps(capabilities.plan_schema(catalogue, "ses_a", context))

    assert json.dumps(URI_PATTERN).strip('"') in schema
    assert (
        "never a reference. To play or queue what an earlier step found, give it as `track`."
        in (schema)
    )


@pytest.mark.parametrize("uri", ["$found", "  $found[2]"])
def test_a_reference_that_reaches_the_operation_as_a_uri_is_still_refused(uri: str) -> None:
    """The schema is the first line of defence; a call arriving by another route meets the
    same refusal, whitespace and all."""
    run = SimpleNamespace(input={"uri": uri}, ctx=None)

    with pytest.raises(MusicInputError) as refused:
        _named(run)  # type: ignore[arg-type]

    assert str(refused.value) == NOT_A_REFERENCE.format(uri=uri.strip())
    assert '{"track": "' + uri.strip() + '"}' in str(refused.value)


def test_the_prompt_shows_find_and_play_in_one_plan() -> None:
    assert '"input": {"track": "$found"}' in read_capability_doc("music")


# --- a queue of several tracks answers per track, inside the step's ceiling ------------------

PLAYED = tuple(
    Track(name=name, artists=("Asake",), uri=f"spotify:track:{index}")
    for index, name in enumerate(
        ("Lonely At The Top", "Terminator", "Sungba", "Joha", "Organise", "Peace Be Unto You"),
        start=1,
    )
)
RECENT = {"id": "recent", "op": "music.recent", "input": {"limit": 6}}


class Clock:
    """A clock the player moves, so a slow queue takes seconds without waiting for any."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Player(FakeMusicClient):
    """A player that refuses named tracks and takes a set number of seconds per queue."""

    def __init__(self, clock: Clock, *, seconds_per_queue: float = 0.0) -> None:
        super().__init__()
        self.clock = clock
        self.seconds_per_queue = seconds_per_queue
        self.refused: dict[str, Exception] = {}

    async def queue(self, profile: str, uri: str, *, device_id: str = "") -> NowPlaying:
        self.clock.now += self.seconds_per_queue
        if uri in self.refused:
            raise self.refused[uri]
        return await super().queue(profile, uri, device_id=device_id)


def player(clock: Clock, *, seconds_per_queue: float = 0.0) -> Player:
    fake = Player(clock, seconds_per_queue=seconds_per_queue)
    fake.seed(plays=tuple(Play(track) for track in PLAYED))
    fake.state = NowPlaying(track=PLAYED[0], progress_ms=5_000, is_playing=True)
    return fake


async def queue_recent(fake: Player) -> dict[str, object]:
    capabilities, context = setup(fake, fake.clock)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [RECENT, {"id": "queue", "op": "music.queue", "input": {"track": "$recent"}}]},
        context,
    )
    assert result["issues"] is None
    assert result["steps"][1]["status"] == "ok"
    return dict(result["steps"][1]["data"])


async def test_a_queue_refused_part_way_reports_every_track_and_queues_the_rest() -> None:
    """The bug, named: a six-track queue whose third track the service refused ended the step
    with that one failure, so the model could not tell the person the first two were queued,
    and the last three were never tried."""
    fake = player(Clock())
    fake.refused["spotify:track:3"] = DownstreamError("music", 403, "not available here")

    answer = await queue_recent(fake)

    assert [(item["track"], item["queued"]) for item in answer["queued"]] == [
        ("Lonely At The Top", True),
        ("Terminator", True),
        ("Sungba", False),
        ("Joha", True),
        ("Organise", True),
        ("Peace Be Unto You", True),
    ]
    assert answer["queued"][2]["reason"] == "not available here"
    assert "note" not in answer
    assert answer["track"]["name"] == "Lonely At The Top"
    assert [uri for _profile, uri, _device in fake.queued] == [
        "spotify:track:1",
        "spotify:track:2",
        "spotify:track:4",
        "spotify:track:5",
        "spotify:track:6",
    ]


async def test_a_queue_stops_before_the_step_s_ceiling_and_says_what_was_left() -> None:
    """The bug, named: six queue commands at five seconds each ran past the thirty-second
    step ceiling, so the step timed out and the model was told nothing about the five tracks
    that had been queued."""
    fake = player(Clock(), seconds_per_queue=5.0)
    capabilities, context = setup(fake, fake.clock)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [RECENT, {"id": "queue", "op": "music.queue", "input": {"track": "$recent"}}]},
        context,
    )

    assert context.step_seconds == 30.0
    answer = result["steps"][1]["data"]
    assert [item["queued"] for item in answer["queued"]] == [True] * 5 + [False]
    assert answer["queued"][5] == {
        "track": "Peace Be Unto You",
        "uri": "spotify:track:6",
        "queued": False,
        "reason": NOT_TRIED,
    }
    assert list(result["steps"][1]["notices"]) == [
        "Queuing stopped after 5 of 6 so this step could answer before its time ran out; "
        "1 left unqueued. Queue those in a new step."
    ]
    assert "note" not in answer
    assert "confirmed" not in answer
    assert len(fake.queued) == 5
    assert fake.clock.now - 100.0 + 5.0 > context.step_seconds - STEP_MARGIN_SECONDS


async def test_a_queue_that_loses_the_service_reports_what_was_queued_before_it() -> None:
    fake = player(Clock())
    fake.refused["spotify:track:2"] = TransportError("connection reset", audience="music")

    answer = await queue_recent(fake)

    assert [item["queued"] for item in answer["queued"]] == [
        True,
        False,
        False,
        False,
        False,
        False,
    ]
    assert answer["queued"][1]["reason"] == UNREACHABLE
    assert {item["reason"] for item in answer["queued"][2:]} == {NOT_REACHED}
    assert len(fake.queued) == 1


async def test_a_queue_with_nothing_queued_still_answers_with_what_is_playing() -> None:
    fake = player(Clock())
    fake.refused["spotify:track:1"] = DownstreamError("music", 404)
    capabilities, context = setup(fake, fake.clock)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [RECENT, {"id": "queue", "op": "music.queue", "input": {"track": "$recent[1]"}}]},
        context,
    )

    answer = result["steps"][1]["data"]
    assert answer["queued"] == [
        {
            "track": "Lonely At The Top",
            "uri": "spotify:track:1",
            "queued": False,
            "reason": "music answered 404",
        }
    ]
    assert answer["track"]["name"] == "Lonely At The Top"
    assert answer["is_playing"] is True


async def test_an_unconfirmed_queue_of_several_tracks_says_so_once_and_per_track() -> None:
    fake = player(Clock())
    fake.confirms = False

    answer = await queue_recent(fake)

    assert answer["confirmed"] is False
    assert "note" not in answer
    assert all(item["queued"] and item["confirmed"] is False for item in answer["queued"])


# --- what a track reference can name ----------------------------------------------------------


async def play_after_recent(given: dict[str, object]) -> FakeMusicClient:
    fake = FakeMusicClient()
    fake.seed(plays=tuple(Play(track) for track in PLAYED))
    capabilities, context = setup(fake)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [RECENT, {"id": "play", "op": "music.play", "input": given}]}, context
    )
    assert result["issues"] is None
    assert [step["status"] for step in result["steps"]] == ["ok", "ok"]
    return fake


async def test_a_reference_with_an_ordinal_plays_that_one_track() -> None:
    fake = await play_after_recent({"track": "$recent[2]"})
    assert fake.played == [("personal", ("spotify:track:2",), "")]


async def test_a_whole_reference_plays_every_track_the_step_returned_in_order() -> None:
    fake = await play_after_recent({"track": "$recent"})
    assert fake.played == [("personal", tuple(track.uri for track in PLAYED), "")]


async def test_a_null_track_beside_a_uri_plays_the_uri() -> None:
    """A strict tool schema lists every property, so a model sends ``null`` for the one it
    does not use; that is not two ways of naming a track."""
    fake = await play_after_recent({"track": None, "uri": "spotify:track:4"})
    assert fake.played == [("personal", ("spotify:track:4",), "")]


def test_the_music_skill_teaches_the_plan_the_capability_page_shows() -> None:
    """The bug, named: the skill an MCP client loads still said ``music.play`` starts a
    track by itself, so a client following it played the literal text ``$found``."""
    body = next(skill.body for skill in CATALOGUE if skill.name == "music")
    assert '"input": {"track": "$found"}' in body
    assert '"input": {"track": "$found"}' in read_capability_doc("music")
