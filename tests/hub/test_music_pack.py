"""Music is gated by connection state and projected before the model sees it."""

from dataclasses import replace

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.spotify import CONFIRM_WAIT_SECONDS, Device, FakeSpotifyClient, Play, Track
from lucy_api.packs.base import Availability, Bound, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.packs.music import UNCONFIRMED_NOTE, MusicPack, _optional_int
from lucy_api.packs.registry import limits_for
from lucy_api.packs.service import Capabilities
from lucy_api.prompt.docs import capability_doc
from lucy_api.sessions.scope import SessionScope


def setup(fake: FakeSpotifyClient) -> tuple[Capabilities, object]:
    capabilities = Capabilities((HelpPack(), MusicPack("http://music.test", client=fake)))
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
    fake = FakeSpotifyClient()
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
    fake = FakeSpotifyClient()
    capabilities, context = setup(fake)

    catalogue = await capabilities.probe(context)
    names = {tool["name"] for tool in capabilities.tools(catalogue, "ses_a")["tools"]}

    assert {"music.find", "music.nowPlaying", "music.devices", "music.recent"} <= names
    assert {"music.play", "music.queue", "music.pause"} <= names


async def test_music_reads_and_writes_use_the_session_profile_and_small_projections() -> None:
    fake = FakeSpotifyClient()
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
    fake = FakeSpotifyClient()
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
    pack = MusicPack("http://music.test", client=FakeSpotifyClient())

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
    fake = FakeSpotifyClient()
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
    fake = FakeSpotifyClient()
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
        assert answer["note"] == UNCONFIRMED_NOTE
        assert answer["track"]["name"] == "Reverie"
        assert answer["progress_ms"] == 64_000
    assert fake.played == [("personal", ("spotify:track:1",), "")]


async def test_a_confirmed_command_carries_no_note() -> None:
    fake = FakeSpotifyClient()
    capabilities, context = setup(fake)
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "pause", "op": "music.pause", "input": {}}]}, context
    )

    assert set(result["steps"][0]["data"]) == {"track", "progress_ms", "is_playing"}


def test_a_music_step_outlasts_the_wait_for_a_confirmed_command() -> None:
    """The bug, named: the client can wait as long as it likes, but the step around it gave up
    at ten seconds, so a play still being confirmed was reported to the model as a failure."""
    pack = MusicPack("http://music.test", client=FakeSpotifyClient())
    limits = limits_for((Bound(pack=pack, availability=Availability(state=State.ready)),))
    assert limits["stepTimeoutMs"] > CONFIRM_WAIT_SECONDS * 1_000


def test_a_boolean_year_is_not_treated_as_a_year() -> None:
    assert _optional_int(True) is None
    assert _optional_int(1999) == 1999
