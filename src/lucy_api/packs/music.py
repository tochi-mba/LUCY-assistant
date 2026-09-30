"""Playback and listening history, exposed as the product word ``music``.

The pack is connection-gated: an unavailable account removes every music operation from
the model's registry, while the capability catalogue keeps a setup path visible to the
person. Every result is projected to names, artists, albums, device labels and playable
URIs before it crosses the tool boundary.

A track found by ``music.find`` is played or queued by reference: ``{"track": "$found"}``,
resolved by the runtime to the tracks that step returned. The documentation always showed
play that way and the operation took only a ``uri`` string, so the plan a model naturally
writes -- find, then play what was found -- sent the literal text ``$found`` as a URI. A
``uri`` is still accepted for a track the model already holds.

A queue of several tracks answers per track. The service takes one track per queue command
and answers each once the provider has accepted it -- a queue changes nothing the player
state reports, so there is nothing to confirm it against -- so a six-track queue is six
commands in a row; one refused part way used to end the step with one failure that hid what
had already been queued, and six slow ones ran past the step's ceiling, so the step timed
out and nothing was reported. Each track now says whether it was queued and, if not, why; the loop
stops before the next command would run past the ceiling and says how many were left.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema import ref
from weftai.schema.spec import integer_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.music import (
    AUDIENCE,
    DEFAULT_RECENT,
    HttpMusicClient,
    UnconfirmedError,
    Wanted,
)
from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Permission, SetupPlan, SetupStep, State
from lucy_api.packs.collections import TRACK
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.music import MusicClient, NowPlaying, Track
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


TRACK_REFERENCE = (
    "The track(s) an earlier music.find or music.recent step returned, by reference: "
    '"$found" for all of them, or "$found[2]" for one.'
)
URI = (
    "A track's uri, as music.find returned it -- never a reference. To play or queue what an "
    "earlier step found, give it as `track`."
)
URI_PATTERN = r"^(?!\s*\$)[\s\S]*$"
"""What `uri` accepts: anything that does not begin, after optional whitespace, with ``$``.

A reference belongs in `track`. Refusing it at validation, where the model reads the
schema, is the first line of defence; `_named` strips and looks again, for a call that
reaches the operation by another route.
"""
NOT_A_REFERENCE = (
    "`uri` takes a track's uri, not a reference; {uri!r} looks like one. To play or queue what "
    'an earlier step found, give it as `track`: {{"track": "{uri}"}}.'
)
EITHER = "Give `track` or `uri`, not both."
NOTHING_FOUND = "The referenced step found no track to play; find one first."
QUEUE_WHAT = "Name what to queue: `track` for a track found earlier, or `uri`."
OUT_OF_TIME_NOTE = (
    "Queuing stopped after {done} of {total} so this step could answer before its time ran "
    "out; {left} left unqueued. Queue those in a new step."
)
"""Why a long queue was cut short, and what to do about the rest.

The step's ceiling is one figure for every step alike, and each queue command may wait as
long as the service takes to confirm it. Running past the ceiling ended the step with a
timeout and no report, so the model could not tell the person which tracks were queued.
"""
NOT_TRIED = "not tried: the step was out of time"
UNREACHABLE = "music could not be reached"
NOT_REACHED = f"not tried: {UNREACHABLE}"


class MusicInputError(ValueError):
    """A play or queue that names its track in a way the operation cannot use."""


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
        client: MusicClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client
        self._clock = clock or time.monotonic

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

    def result_trust(self, operation: str, data: object) -> Trust:
        """Track and playlist names are chosen by other people."""
        del operation, data
        return Trust.untrusted

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
                    "description": (
                        "Play the track(s) an earlier step found, as `track`, or one by `uri`; "
                        "resume what was loaded when neither is given. Optionally on a named "
                        "device."
                    ),
                    "input": object_schema(
                        {
                            "track": ref(TRACK, description=TRACK_REFERENCE).optional(),
                            "uri": string_schema().regex(URI_PATTERN).describe(URI).optional(),
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
                    "description": (
                        "Queue the track(s) an earlier step found, as `track`, or one by `uri`, "
                        "behind the current item. Several are queued in order and reported one "
                        "by one."
                    ),
                    "input": object_schema(
                        {
                            "track": ref(TRACK, description=TRACK_REFERENCE).optional(),
                            "uri": string_schema().regex(URI_PATTERN).describe(URI).optional(),
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

    def _client(self, context: PackContext) -> MusicClient:
        return self._override or HttpMusicClient(
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
        uris = tuple(uri for uri, _name in _named(run))
        return await _commanded(
            self._client(run.ctx).play(run.ctx.profile, uris=uris, device_id=_device_id(run))
        )

    async def _queue(self, run: RunContext[PackContext]) -> dict[str, Any]:
        """Queue each track in turn and answer for every one of them.

        One at a time, in order: each queue answers once the provider has accepted it, and the
        next must go behind it. The loop stops when the next command, taking as long as the slowest
        so far, would run past the step's ceiling; the report says what was left.
        """
        named = _named(run)
        if not named:
            raise MusicInputError(QUEUE_WHAT)
        client = self._client(run.ctx)
        device = _device_id(run)
        budget = run.ctx.within_step(run.ctx.step_seconds)
        started = self._clock()
        longest = 0.0
        seen: NowPlaying | None = None
        report: list[dict[str, Any]] = []
        for index, (uri, name) in enumerate(named):
            if index and self._clock() - started + longest > budget:
                report.extend(_untried(named[index:], NOT_TRIED))
                break
            began = self._clock()
            try:
                seen = await client.queue(run.ctx.profile, uri, device_id=device)
            except UnconfirmedError as unconfirmed:
                seen = unconfirmed.observed
                report.append({"track": name, "uri": uri, "queued": True, "confirmed": False})
            except DownstreamError as refused:
                report.append(_not_queued(uri, name, refused.detail or str(refused)))
            except TransportError:
                report.append(_not_queued(uri, name, UNREACHABLE))
                report.extend(_untried(named[index + 1 :], NOT_REACHED))
                break
            else:
                report.append({"track": name, "uri": uri, "queued": True})
            longest = max(longest, self._clock() - began)
        if seen is None:
            seen = await client.now_playing(run.ctx.profile)
        return {**_playing(seen), "queued": report, **_queue_notes(report, len(named))}

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


def _queue_notes(report: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """What a queue's answer says beyond its per-track report: unconfirmed, or cut short."""
    unconfirmed = any(item.get("confirmed") is False for item in report)
    notes = [UNCONFIRMED_NOTE] if unconfirmed else []
    left = sum(1 for item in report if item.get("reason") == NOT_TRIED)
    if left:
        notes.append(OUT_OF_TIME_NOTE.format(done=total - left, total=total, left=left))
    answer: dict[str, Any] = {}
    if unconfirmed:
        answer["confirmed"] = False
    if notes:
        answer["note"] = " ".join(notes)
    return answer


def _untried(rest: tuple[tuple[str, str], ...], reason: str) -> list[dict[str, Any]]:
    return [_not_queued(uri, name, reason) for uri, name in rest]


def _not_queued(uri: str, name: str, reason: str) -> dict[str, Any]:
    return {"track": name, "uri": uri, "queued": False, "reason": reason}


def _named(run: RunContext[PackContext]) -> tuple[tuple[str, str], ...]:
    """The tracks a play or queue names, as (uri, name): every one the reference resolved
    to, or the one uri, which is its own name."""
    uri = str(run.input.get("uri") or "").strip()
    picked = run.input.get("track")
    if picked is not None and uri:
        raise MusicInputError(EITHER)
    if uri.startswith("$"):
        raise MusicInputError(NOT_A_REFERENCE.format(uri=uri))
    if picked is None:
        return ((uri, uri),) if uri else ()
    named = tuple(
        (str(item["uri"]), str(item.get("name") or item["uri"]))
        for item in picked.items
        if isinstance(item, dict) and item.get("uri")
    )
    if not named:
        raise MusicInputError(NOTHING_FOUND)
    return named


def _device_id(run: RunContext[PackContext]) -> str:
    given = str(run.input.get("device_id") or "")
    if given:
        return given
    default = run.ctx.defaults.get("music.device_id")
    return str(default) if isinstance(default, str) else ""


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["MusicInputError", "MusicPack"]
