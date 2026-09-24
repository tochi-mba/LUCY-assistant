"""Music projections keep the five fields a person would say, and drop the rest."""

from __future__ import annotations

import pytest

from lucy_api.clients.errors import NotConnectedError
from lucy_api.clients.spotify import (
    CONFIRM_WAIT_SECONDS,
    Device,
    FakeSpotifyClient,
    HttpSpotifyClient,
    Play,
    Track,
    Wanted,
    _item,
    _track,
)
from lucy_api.clients.testing import Answer, FakeHttp, problem

SIBLING_CONFIRM_SECONDS = 15.0
"""Spotify-api's `confirm_timeout_seconds`: its config.py default, which preferences.py
lets a person narrow and never raise."""


def test_a_lookup_item_omits_hints_that_were_not_given() -> None:
    assert _item(Wanted(name="Prelude")) == {"name": "Prelude"}
    assert _item(Wanted(name="Prelude", artist="Debussy", album="Images", year=1905)) == {
        "name": "Prelude",
        "artist": "Debussy",
        "album": "Images",
        "year": 1905,
    }


def test_an_empty_track_payload_is_not_a_track() -> None:
    assert _track(None) is None
    assert _track({}) is None
    assert _track("nope") is None


async def test_connected_is_false_when_the_credential_is_missing() -> None:
    http = FakeHttp(
        problem(502, code="credential-unavailable", detail="not linked"),
        problem(502, code="credential-unavailable", detail="not linked"),
    )
    client = HttpSpotifyClient(http, "http://music.test")

    assert await client.connected("personal") is False
    with pytest.raises(NotConnectedError):
        await client.devices("personal")


async def test_player_calls_project_and_use_the_music_audience() -> None:
    track = {
        "name": "Prelude",
        "artists": [{"name": "Debussy"}],
        "album": {"name": "Images"},
        "uri": "spotify:track:1",
        "duration_ms": 1000,
        "available_markets": ["US"],
    }
    devices = {"devices": [{"id": "d1", "name": "Kitchen", "type": "Speaker", "is_active": True}]}
    http = FakeHttp(
        Answer(body=devices),
        Answer(body=devices),
        Answer(body={"item": track, "progress_ms": 12, "is_playing": True, "shuffle_state": True}),
        Answer(
            body={
                "items": [
                    {"track": track, "played_at": "2026-09-17T12:00:00Z"},
                    {"track": {}, "played_at": "2026-09-17T11:00:00Z"},
                ]
            }
        ),
        Answer(
            body={
                "results": [
                    {"index": 0, "status": "found", "track": track},
                    {"index": 1, "status": "error", "error": "no match"},
                ]
            }
        ),
        Answer(body={"item": track, "is_playing": True}),
        Answer(body={"item": track, "is_playing": True}),
        Answer(body={"item": track, "is_playing": False}),
        Answer(body={"item": track, "is_playing": True}),
    )
    client = HttpSpotifyClient(http, "http://music.test")

    assert await client.connected("work") is True
    devices = await client.devices("work")
    playing = await client.now_playing("work")
    recent = await client.recent("work", limit=5)
    found = await client.find(
        [Wanted(name="Prelude"), Wanted(name="Missing")],
        profile="work",
        market="GB",
    )
    await client.play("work", uris=["spotify:track:1"], device_id="d1")
    await client.queue("work", "spotify:track:1", device_id="d1")
    paused = await client.pause("work", device_id="d1")
    resumed = await client.play("work")

    assert devices[0].name == "Kitchen"
    assert playing.track is not None
    assert playing.track.uri == "spotify:track:1"
    assert len(recent) == 1
    assert found[1].detail == "no match"
    assert paused.is_playing is False
    assert resumed.is_playing is True
    assert all(call.audience == "spotify-api" for call in http.calls)
    assert http.calls[4].json["market"] == "GB"


async def test_the_in_memory_player_records_the_profile_on_every_call() -> None:
    fake = FakeSpotifyClient()
    track = Track(name="Prelude", uri="spotify:track:1")
    fake.stock("Prelude", track)
    fake.seed(devices=(Device("d1", "Kitchen", "speaker", True),), plays=(Play(track=track),))

    assert await fake.connected("work") is True
    fake.is_connected = False
    assert await fake.connected("work") is False
    fake.is_connected = True
    assert (await fake.devices("work"))[0].device_id == "d1"
    assert (await fake.recent("work", limit=1))[0].track.name == "Prelude"
    found = await fake.find(
        [Wanted(name="Prelude"), Wanted(name="Nope")], profile="work", market="GB"
    )
    playing = await fake.play("work", uris=["spotify:track:1"], device_id="d1")
    queued = await fake.queue("work", "spotify:track:1", device_id="d1")
    paused = await fake.pause("work", device_id="d1")

    assert found[0].status == "found"
    assert found[1].status == "not_found"
    assert playing.is_playing is True
    assert queued.is_playing is True
    assert paused.is_playing is False
    assert fake.asked[-1] == "work"
    assert fake.played[0] == ("work", ("spotify:track:1",), "d1")


# --- a player command is waited on for as long as it takes to confirm ---------------------------
#
# Spotify-api answers a play, queue or pause only once it has seen the effect on the player, for
# up to its `confirm_timeout_seconds`, and a person's setting can only narrow that. Against the
# turn's ten-second default a device slow to wake was abandoned while it was still confirming,
# and the command was sent again, restarting the track it had just started.


async def test_a_player_command_is_waited_on_longer_than_the_service_takes_to_confirm_it() -> None:
    """The bug, named: play, queue and pause carried no timeout of their own, so a device that
    took twelve seconds to confirm was given up on at ten while its track was starting."""
    http = FakeHttp(*(Answer(body={"is_playing": True}) for _ in range(3)))
    client = HttpSpotifyClient(http, "http://music.test")

    await client.play("work", uris=["spotify:track:1"])
    await client.queue("work", "spotify:track:1")
    await client.pause("work")

    assert [call.timeout_seconds for call in http.calls] == [CONFIRM_WAIT_SECONDS] * 3
    assert CONFIRM_WAIT_SECONDS > SIBLING_CONFIRM_SECONDS


async def test_a_read_keeps_the_turn_s_ordinary_wait() -> None:
    http = FakeHttp(Answer(body={"is_playing": False}))
    await HttpSpotifyClient(http, "http://music.test").now_playing("work")
    assert http.last.timeout_seconds is None
