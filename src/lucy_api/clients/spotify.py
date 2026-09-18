"""Music, reduced to the seven fields a person would say out loud.

A Spotify player read is one of the largest documents any sibling returns: a track carries
its available markets, its external ids, its images in three sizes, its album's artists and
their own ids, and the whole thing again under `context`. None of that helps a model say
"Prelude by Debussy, halfway through", and all of it is charged to a context window every
turn somebody asks what is playing.

So this client projects, hard, to exactly the shape the plan names:
`{name, artists[].name, album.name, uri, duration_ms, progress_ms, is_playing}`. The uri is
kept because it is what `play` and `queue` take -- a projection that dropped it would force
a second round trip to do anything with the answer -- and nothing else survives.

The probe is `connected`, and it is the one read that can answer cheaply: there is no
readiness endpoint for a person's music, and a device list answers `502
credential-unavailable` when nobody has connected an account. That status is what flips the
capability into `not_connected`, which is how a person gets a link rather than an apology.

Every write here confirms itself downstream -- Spotify's own 204 means "command accepted",
not "audio is playing" -- so each one answers with the player state in which the effect was
observed, and that state comes back through the same projection as a read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.errors import NotConnectedError
from lucy_api.clients.transport import Sibling, field, flag, moment, nested, number, rows, text

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from lucy_api.packs.context import Http

SERVICE = "spotify"
AUDIENCE = "spotify-api"

DEFAULT_RECENT = 10
"""How many plays a history read asks for. Small on purpose: the service ceiling is 50."""


@dataclass(frozen=True, slots=True)
class Track:
    """One piece of music, as a person would describe it."""

    name: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    uri: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """What is playing and how far in. `track` is `None` when nothing is loaded."""

    track: Track | None = None
    progress_ms: int = 0
    is_playing: bool = False


@dataclass(frozen=True, slots=True)
class Device:
    """Somewhere audio can come out, named the way the person named it."""

    device_id: str
    name: str
    kind: str = ""
    is_active: bool = False


@dataclass(frozen=True, slots=True)
class Play:
    """One thing that was listened to, and when."""

    track: Track
    played_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Wanted:
    """One loosely-described track to resolve. Only the name is required."""

    name: str
    artist: str = ""
    album: str = ""
    year: int | None = None


@dataclass(frozen=True, slots=True)
class Found:
    """What became of one requested track.

    A batch answers with one of these per item, in the order they were submitted, and a
    single unresolvable track never costs the other forty-nine. `detail` is the service's
    sentence about why, present only when `status` is `error`.
    """

    index: int
    status: str
    track: Track | None = None
    detail: str = ""


class SpotifyClient(Protocol):
    """The music operations Lucy binds, and nothing that would need a second round trip."""

    async def connected(self, profile: str) -> bool:
        """Whether this profile has a usable music account right now."""
        ...

    async def devices(self, profile: str) -> tuple[Device, ...]:
        """Where this account can play."""
        ...

    async def now_playing(self, profile: str) -> NowPlaying:
        """What is playing, projected to the fields a person would say."""
        ...

    async def recent(self, profile: str, *, limit: int = DEFAULT_RECENT) -> tuple[Play, ...]:
        """Listening history, newest first."""
        ...

    async def find(
        self, wanted: Sequence[Wanted], *, profile: str, market: str = ""
    ) -> tuple[Found, ...]:
        """Resolve loosely-described tracks into playable ones, in one call."""
        ...

    async def play(
        self, profile: str, *, uris: Sequence[str] = (), device_id: str = ""
    ) -> NowPlaying:
        """Start or resume playback and answer with the state that confirms it."""
        ...

    async def queue(self, profile: str, uri: str, *, device_id: str = "") -> NowPlaying:
        """Add one track behind whatever is playing."""
        ...

    async def pause(self, profile: str, *, device_id: str = "") -> NowPlaying:
        """Stop playback and answer with the state that confirms it."""
        ...


class HttpSpotifyClient:
    """The real client. Every method answers with a projection, never with a payload."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def connected(self, profile: str) -> bool:
        """The probe: a device list, read only for whether it was refused for a credential.

        There is no readiness endpoint that speaks about one person, and keyring's status is
        the other half of this answer. What this adds is the case keyring cannot see: a
        grant that still exists and no longer works.
        """
        try:
            await self.devices(profile)
        except NotConnectedError:
            return False
        return True

    async def devices(self, profile: str) -> tuple[Device, ...]:
        """Every device this account can reach, without the private-session flags."""
        payload = await self._api.send("GET", "/v1/player/devices", profile=profile)
        return tuple(_device(row) for row in rows(payload, "devices"))

    async def now_playing(self, profile: str) -> NowPlaying:
        """The player state, projected. Nothing playing is a state, not an absence."""
        payload = await self._api.send("GET", "/v1/player", profile=profile)
        return _now_playing(payload)

    async def recent(self, profile: str, *, limit: int = DEFAULT_RECENT) -> tuple[Play, ...]:
        """Recent plays, each one a track and a timestamp and nothing else."""
        payload = await self._api.send(
            "GET", "/v1/player/recently-played", params={"limit": limit}, profile=profile
        )
        plays = (
            (_track(nested(row, "track")), moment(field(row, "played_at")))
            for row in rows(payload, "items")
        )
        # A history entry with no track is not a play. Dropping it here keeps the type
        # honest, so nothing downstream has to render an empty track it cannot name.
        return tuple(Play(track=track, played_at=at) for track, at in plays if track is not None)

    async def find(
        self, wanted: Sequence[Wanted], *, profile: str, market: str = ""
    ) -> tuple[Found, ...]:
        """Resolve a batch. Partial success is the contract, so every item gets an answer."""
        body: dict[str, Any] = {"items": [_item(one) for one in wanted]}
        if market:
            body["market"] = market
        payload = await self._api.send("POST", "/v1/lookup", body=body, profile=profile)
        return tuple(_found(row) for row in rows(payload, "results"))

    async def play(
        self, profile: str, *, uris: Sequence[str] = (), device_id: str = ""
    ) -> NowPlaying:
        """Play named tracks, or resume what was loaded when none are named."""
        body: dict[str, Any] = {}
        if uris:
            body["uris"] = list(uris)
        if device_id:
            body["device_id"] = device_id
        return _now_playing(
            await self._api.send("POST", "/v1/player/play", body=body, profile=profile)
        )

    async def queue(self, profile: str, uri: str, *, device_id: str = "") -> NowPlaying:
        """Queue one track, which deliberately does not interrupt what is playing."""
        body: dict[str, Any] = {"uri": uri}
        if device_id:
            body["device_id"] = device_id
        return _now_playing(
            await self._api.send("POST", "/v1/player/queue", body=body, profile=profile)
        )

    async def pause(self, profile: str, *, device_id: str = "") -> NowPlaying:
        """Pause, and answer with the state in which the pause was observed."""
        params = {"device_id": device_id} if device_id else None
        return _now_playing(
            await self._api.send("POST", "/v1/player/pause", params=params, profile=profile)
        )


def _item(wanted: Wanted) -> dict[str, Any]:
    """One lookup item, with the hints that were actually given.

    The optional fields are omitted rather than sent as null: each one that is present
    narrows the search, and an explicit null narrows nothing while being one more thing the
    service has to decide what to do with.
    """
    body: dict[str, Any] = {"name": wanted.name}
    if wanted.artist:
        body["artist"] = wanted.artist
    if wanted.album:
        body["album"] = wanted.album
    if wanted.year is not None:
        body["year"] = wanted.year
    return body


def _track(payload: Any) -> Track | None:
    """A track, or `None` when there is not one. Seven fields in, five fields out."""
    if not isinstance(payload, dict) or not payload:
        return None
    return Track(
        name=text(payload, "name"),
        artists=tuple(text(artist, "name") for artist in rows(payload, "artists")),
        album=text(nested(payload, "album"), "name"),
        uri=text(payload, "uri"),
        duration_ms=number(payload, "duration_ms"),
    )


def _now_playing(payload: Any) -> NowPlaying:
    """The player state, with the device, the context and the permitted actions dropped."""
    return NowPlaying(
        track=_track(nested(payload, "item")),
        progress_ms=number(payload, "progress_ms"),
        is_playing=flag(payload, "is_playing"),
    )


def _device(row: Any) -> Device:
    """One device: which one, what it is called, and whether it is the active one."""
    return Device(
        device_id=text(row, "id"),
        name=text(row, "name"),
        kind=text(row, "type"),
        is_active=flag(row, "is_active"),
    )


def _found(row: Any) -> Found:
    """One lookup outcome, carrying the service's reason when there is one."""
    return Found(
        index=number(row, "index"),
        status=text(row, "status"),
        track=_track(nested(row, "track")),
        detail=text(row, "error"),
    )


class FakeSpotifyClient:
    """An in-memory player, so a music pack's tests need no account and no network.

    `is_connected` is a flag rather than a consequence of the seeded data: "the person has
    not connected music" is the state most worth testing and the one that has no natural
    representation in a list of devices.

    Every method records the profile it was called with. Which credential set a call used is
    invisible in its answer and is exactly the thing a multi-profile bug gets wrong.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self.state = NowPlaying()
        self.known: tuple[Device, ...] = ()
        self.history: tuple[Play, ...] = ()
        self.catalogue: dict[str, Track] = {}
        self.asked: list[str] = []
        self.played: list[tuple[str, tuple[str, ...], str]] = []
        self.queued: list[tuple[str, str, str]] = []
        self.paused: list[tuple[str, str]] = []

    def seed(self, *, devices: Iterable[Device] = (), plays: Iterable[Play] = ()) -> None:
        """Set what this account can play on and what it has played."""
        self.known = tuple(devices)
        self.history = tuple(plays)

    def stock(self, query: str, track: Track) -> None:
        """Make one name resolvable, so `find` has something to find."""
        self.catalogue[query] = track

    async def connected(self, profile: str) -> bool:
        """Whatever the test said. The flag is the whole point of this method."""
        self.asked.append(profile)
        return self.is_connected

    async def devices(self, profile: str) -> tuple[Device, ...]:
        """The seeded devices."""
        self.asked.append(profile)
        return self.known

    async def now_playing(self, profile: str) -> NowPlaying:
        """Whatever the last write left playing."""
        self.asked.append(profile)
        return self.state

    async def recent(self, profile: str, *, limit: int = DEFAULT_RECENT) -> tuple[Play, ...]:
        """The seeded history, honouring the limit so a paging test means something."""
        self.asked.append(profile)
        return self.history[:limit]

    async def find(
        self, wanted: Sequence[Wanted], *, profile: str, market: str = ""
    ) -> tuple[Found, ...]:
        """One answer per item, found or not, in the order they were asked for."""
        self.asked.append(f"{profile}:{market}" if market else profile)
        found: list[Found] = []
        for index, one in enumerate(wanted):
            track = self.catalogue.get(one.name)
            status = "found" if track is not None else "not_found"
            found.append(Found(index=index, status=status, track=track))
        return tuple(found)

    async def play(
        self, profile: str, *, uris: Sequence[str] = (), device_id: str = ""
    ) -> NowPlaying:
        """Record what was asked for and start playing the first of it."""
        self.asked.append(profile)
        self.played.append((profile, tuple(uris), device_id))
        track = next((item for item in self.catalogue.values() if item.uri in uris), None)
        self.state = NowPlaying(track=track or self.state.track, is_playing=True)
        return self.state

    async def queue(self, profile: str, uri: str, *, device_id: str = "") -> NowPlaying:
        """Record the queued uri without disturbing what is playing."""
        self.asked.append(profile)
        self.queued.append((profile, uri, device_id))
        return self.state

    async def pause(self, profile: str, *, device_id: str = "") -> NowPlaying:
        """Stop, keeping whatever track was loaded."""
        self.asked.append(profile)
        self.paused.append((profile, device_id))
        self.state = NowPlaying(track=self.state.track, progress_ms=self.state.progress_ms)
        return self.state


if TYPE_CHECKING:

    def _satisfies(real: HttpSpotifyClient, fake: FakeSpotifyClient) -> tuple[SpotifyClient, ...]:
        """Static proof that both implementations satisfy the seam."""
        return (real, fake)


__all__ = [
    "AUDIENCE",
    "DEFAULT_RECENT",
    "SERVICE",
    "Device",
    "FakeSpotifyClient",
    "Found",
    "HttpSpotifyClient",
    "NowPlaying",
    "Play",
    "SpotifyClient",
    "Track",
    "Wanted",
]
