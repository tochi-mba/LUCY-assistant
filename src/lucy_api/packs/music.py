"""Playback and listening history, exposed as the product word ``music``.

The pack is connection-gated: an unavailable account removes every music operation from
the model's registry, while the capability catalogue keeps a setup path visible to the
person. Every result is projected to names, artists, albums, device labels and playable
URIs before it crosses the tool boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import integer_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.spotify import (
    AUDIENCE,
    DEFAULT_RECENT,
    HttpSpotifyClient,
    UnconfirmedError,
    Wanted,
)
from lucy_api.packs.base import Availability, Permission, SetupPlan, SetupStep, State
from lucy_api.packs.collections import TRACK
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.spotify import NowPlaying, SpotifyClient, Track
    from lucy_api.packs.context import PackContext

UNCONFIRMED_NOTE = (
    "The command was accepted but could not be confirmed in time; this is the last state "
    "seen. Check music.nowPlaying before sending it again."
)
"""What the model is told when a command was accepted and not yet seen to take effect.

Without it the model read an outage, told the person playback had failed while the track may
already have been starting, and sent the command again -- which restarts a track from the
beginning. The last sentence names the read that settles it, rather than the retry.
"""


class MusicPack:
    """Find, inspect and control music without exposing the provider service."""

    id = "music"
    title = "Music"
    summary = "Find music, inspect playback and control the active player."

    def __init__(
        self,
        base_url: str,
        *,
        audience: str = AUDIENCE,
        client: SpotifyClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="music.control",
                title="Control music playback",
                description="Start, queue or pause music on a connected device.",
                risk="write",
                covers=("music.play", "music.queue", "music.pause"),
                outward=True,
            ),
        )

    def setup(self) -> SetupPlan | None:
        return SetupPlan(
            summary="Connect a music account in a browser; never paste a password or token here.",
            steps=(
                SetupStep(
                    id="connect",
                    kind="oauth",
                    title="Connect music",
                    description="Open Lucy's connection link and approve playback access.",
                ),
            ),
        )

    async def probe(self, context: PackContext) -> Availability:
        try:
            connected = await self._client(context).connected(context.profile)
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(state=State.unavailable, detail="music could not be reached")
        if not connected:
            return Availability(
                state=State.not_connected,
                detail="connect music to make playback tools available",
            )
        return Availability(state=State.ready, detail="connected")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        del context
        return (
            define_operation(
                {
                    "name": "music.find",
                    "description": "Find one playable track from its name and optional hints.",
                    "input": object_schema(
                        {
                            "name": string_schema().describe("Track name."),
                            "artist": string_schema().optional(),
                            "album": string_schema().optional(),
                            "year": integer_schema().optional(),
                        }
                    ),
                    "output": TRACK,
                    "effects": "read",
                    "run": self._find,
                }
            ),
            define_operation(
                {
                    "name": "music.nowPlaying",
                    "description": "Read what is playing, whether it is paused, and progress.",
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._now_playing,
                }
            ),
            define_operation(
                {
                    "name": "music.devices",
                    "description": "List available playback devices and identify the active one.",
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._devices,
                }
            ),
            define_operation(
                {
                    "name": "music.recent",
                    "description": "Read recent listening history, newest first.",
                    "input": object_schema({"limit": integer_schema().optional()}),
                    "output": TRACK,
                    "effects": "read",
                    "run": self._recent,
                }
            ),
            define_operation(
                {
                    "name": "music.play",
                    "description": "Play or resume one track, optionally on a named device.",
                    "input": object_schema(
                        {
                            "uri": string_schema().optional(),
                            "device_id": string_schema().optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._play,
                }
            ),
            define_operation(
                {
                    "name": "music.queue",
                    "description": "Queue one playable track behind the current item.",
                    "input": object_schema(
                        {
                            "uri": string_schema().describe("Playable URI returned by music.find."),
                            "device_id": string_schema().optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._queue,
                }
            ),
            define_operation(
                {
                    "name": "music.pause",
                    "description": "Pause playback, optionally on one device.",
                    "input": object_schema({"device_id": string_schema().optional()}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._pause,
                }
            ),
        )

    def _client(self, context: PackContext) -> SpotifyClient:
        return self._override or HttpSpotifyClient(
            context.http, self.base_url, audience=self.audience
        )

    async def _find(self, run: RunContext[PackContext]) -> list[dict[str, Any]]:
        result = await self._client(run.ctx).find(
            (
                Wanted(
                    name=str(run.input.get("name") or ""),
                    artist=str(run.input.get("artist") or ""),
                    album=str(run.input.get("album") or ""),
                    year=_optional_int(run.input.get("year")),
                ),
            ),
            profile=run.ctx.profile,
        )
        return [_track(item.track) for item in result if item.track is not None]

    async def _now_playing(self, run: RunContext[PackContext]) -> dict[str, Any]:
        return _playing(await self._client(run.ctx).now_playing(run.ctx.profile))

    async def _devices(self, run: RunContext[PackContext]) -> dict[str, Any]:
        devices = await self._client(run.ctx).devices(run.ctx.profile)
        return {
            "devices": [
                {
                    "id": device.device_id,
                    "name": device.name,
                    "kind": device.kind,
                    "active": device.is_active,
                }
                for device in devices
            ]
        }

    async def _recent(self, run: RunContext[PackContext]) -> list[dict[str, Any]]:
        limit = max(1, min(int(run.input.get("limit") or DEFAULT_RECENT), 50))
        plays = await self._client(run.ctx).recent(run.ctx.profile, limit=limit)
        return [
            {
                **_track(play.track),
                "played_at": play.played_at.isoformat() if play.played_at else "",
            }
            for play in plays
        ]

    async def _play(self, run: RunContext[PackContext]) -> dict[str, Any]:
        uri = str(run.input.get("uri") or "")
        return await _commanded(
            self._client(run.ctx).play(
                run.ctx.profile,
                uris=(uri,) if uri else (),
                device_id=_device_id(run),
            )
        )

    async def _queue(self, run: RunContext[PackContext]) -> dict[str, Any]:
        return await _commanded(
            self._client(run.ctx).queue(
                run.ctx.profile,
                str(run.input.get("uri") or ""),
                device_id=_device_id(run),
            )
        )

    async def _pause(self, run: RunContext[PackContext]) -> dict[str, Any]:
        return await _commanded(
            self._client(run.ctx).pause(run.ctx.profile, device_id=_device_id(run))
        )


async def _commanded(command: Awaitable[NowPlaying]) -> dict[str, Any]:
    """A write's answer: the state that confirmed it, or the last one seen and why."""
    try:
        state = await command
    except UnconfirmedError as unconfirmed:
        return {**_playing(unconfirmed.observed), "confirmed": False, "note": UNCONFIRMED_NOTE}
    return _playing(state)


def _track(track: Track) -> dict[str, Any]:
    return {
        "name": track.name,
        "artist": ", ".join(track.artists),
        "album": track.album,
        "uri": track.uri,
        "duration_ms": track.duration_ms,
    }


def _playing(state: NowPlaying) -> dict[str, Any]:
    return {
        "track": _track(state.track) if state.track is not None else None,
        "progress_ms": state.progress_ms,
        "is_playing": state.is_playing,
    }


def _device_id(run: RunContext[PackContext]) -> str:
    given = str(run.input.get("device_id") or "")
    if given:
        return given
    default = run.ctx.defaults.get("music.device_id")
    return str(default) if isinstance(default, str) else ""


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["MusicPack"]
