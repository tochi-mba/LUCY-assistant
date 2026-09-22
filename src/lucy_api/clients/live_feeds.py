"""Turn sibling projections into Lucy's standard standing and live feeds.

The services keep ownership of their data contracts.  This module owns the presentation
boundary: it selects the few fields useful on every turn, gives each one a settings key,
and keeps enough provenance or identity to fetch the full record later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lucy_api.clients.errors import AbsentError, DownstreamError, NotConnectedError
from lucy_api.clients.transport import Sibling, field, rows, segment, text
from lucy_api.context.feeds import Feed, FeedEntry, FeedRequest, Volatility
from lucy_api.context.types import Trust

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lucy_api.clients.environments import EnvironmentsClient
    from lucy_api.clients.spotify import SpotifyClient
    from lucy_api.clients.user import UserClient
    from lucy_api.packs.context import Http

PERSONA_AUDIENCE = "persona-api"
PERSONA_SERVICE = "persona"
MAX_LINE_CHARS = 240

_SOURCE_TRUST: Mapping[str, Trust] = {
    "owner": Trust.stated,
    "assistant": Trust.inferred,
    "service": Trust.observed,
}

_ACCOUNT_TRUST: Mapping[str, Trust] = {
    "stated": Trust.stated,
    "inferred": Trust.inferred,
    "observed": Trust.observed,
}


class PersonaFeeds:
    """The assistant identity and pinned persona records for one profile."""

    name = "persona"

    def __init__(self, http: Http, base_url: str, *, audience: str = PERSONA_AUDIENCE) -> None:
        self._api = Sibling(
            http=http, base_url=base_url, service=PERSONA_SERVICE, audience=audience
        )

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]:
        try:
            payload = await self._api.send(
                "GET", f"/v1/personas/{segment(request.profile)}", profile=request.profile
            )
        except AbsentError:
            return ()
        entries = _persona_entries(payload)
        if not entries:
            return ()
        card = field(payload, "persona")
        return (
            Feed(
                id="persona",
                title="assistant identity and pinned notes",
                volatility=Volatility.standing,
                version=text(card, "updated_at"),
                entries=entries,
            ),
        )


@dataclass(frozen=True, slots=True)
class UserFeeds:
    """Pinned account facts as a standing feed, never mixed with memory retrieval."""

    client: UserClient
    name: str = "account"

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]:
        try:
            facts = await self.client.pinned(profile=request.profile)
        except AbsentError:
            return ()
        if not facts:
            return ()
        entries = tuple(
            FeedEntry(
                key=fact.id or f"pinned_{index}",
                setting="pinned",
                line=_bounded(fact.line, reference=fact.id or fact.key),
                trust=_ACCOUNT_TRUST.get(fact.source, Trust.untrusted),
                source=fact.source,
                asserted_by=fact.asserted_by,
                recorded_at=fact.updated_at,
            )
            for index, fact in enumerate(facts, start=1)
        )
        latest = ""
        for fact in facts:
            stamp = fact.updated_at.isoformat() if fact.updated_at else ""
            latest = max(latest, stamp)
        return (
            Feed(
                id="account",
                title="pinned facts about you",
                volatility=Volatility.standing,
                version=latest,
                entries=entries,
            ),
        )


@dataclass(frozen=True, slots=True)
class MusicFeeds:
    """Now-playing and active-device state, fetched together each turn."""

    client: SpotifyClient
    name: str = "music"

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]:
        try:
            playing = await self.client.now_playing(request.profile)
            devices = await self.client.devices(request.profile)
        except NotConnectedError:
            return ()
        entries: list[FeedEntry] = []
        if playing.track is not None:
            artists = ", ".join(playing.track.artists) or "unknown artist"
            state = "playing" if playing.is_playing else "paused"
            progress = _duration(playing.progress_ms)
            duration = _duration(playing.track.duration_ms)
            entries.append(
                FeedEntry(
                    key="now_playing",
                    line=_bounded(
                        f"{state}: {playing.track.name} by {artists}; {progress} of {duration}",
                        reference=playing.track.uri,
                    ),
                    trust=Trust.observed,
                    source="music account",
                )
            )
        if playing.shuffled is not None:
            entries.append(
                FeedEntry(
                    key="shuffled",
                    line="queue is shuffled" if playing.shuffled else "queue is in order",
                    trust=Trust.observed,
                    source="music account",
                )
            )
        if playing.repeat:
            entries.append(
                FeedEntry(
                    key="repeat",
                    line=f"repeat: {playing.repeat}",
                    trust=Trust.observed,
                    source="music account",
                )
            )
        active = next((device for device in devices if device.is_active), None)
        if active is not None:
            kind = f" ({active.kind})" if active.kind else ""
            entries.append(
                FeedEntry(
                    key="device",
                    line=_bounded(
                        f"active device: {active.name}{kind}", reference=active.device_id
                    ),
                    trust=Trust.observed,
                    source="music account",
                )
            )
        if not entries:
            return ()
        return (
            Feed(
                id="music",
                title="player state right now",
                volatility=Volatility.live,
                entries=tuple(entries),
                trust=Trust.observed,
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceFeeds:
    """The attached workspace's observable state, without its host path."""

    client: EnvironmentsClient
    environment_id: str
    workspace_rel: str = ""
    name: str = "workspace"

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]:
        environments = await self.client.environments(profile=request.profile)
        current = next(
            (item for item in environments if item.environment_id == self.environment_id), None
        )
        if current is None:
            return ()
        entries: list[FeedEntry] = [
            FeedEntry(
                key="shells_running",
                line=(
                    f"{current.shells_running} shells running; "
                    f"environment {current.state or 'unknown'}"
                ),
                trust=Trust.observed,
                source="workspace",
            ),
            FeedEntry(
                key="sandbox",
                line=f"sandbox isolation: {current.sandbox_tier or 'unknown'}",
                trust=Trust.observed,
                source="workspace",
            ),
        ]
        if self.workspace_rel:
            entries.append(
                FeedEntry(
                    key="cwd",
                    line=f"working directory: {self.workspace_rel}",
                    trust=Trust.observed,
                    source="workspace",
                )
            )
        branch = await _git_branch(self.client, self.environment_id, self.workspace_rel)
        if branch:
            entries.append(
                FeedEntry(
                    key="git_branch",
                    line=f"git branch: {branch}",
                    trust=Trust.observed,
                    source="workspace",
                )
            )
        return (
            Feed(
                id="workspace",
                title="attached workspace right now",
                volatility=Volatility.live,
                version=current.last_activity_at.isoformat() if current.last_activity_at else "",
                entries=tuple(entries),
                trust=Trust.observed,
            ),
        )


_BACKEND_LABELS = {
    "google": "Google",
    "searxng": "SearXNG",
}


@dataclass(frozen=True, slots=True)
class ResearchFeeds:
    """Which search backend is in force, never a service name."""

    backend: str
    name: str = "research"

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]:
        del request
        label = _BACKEND_LABELS.get(self.backend)
        if not label:
            return ()
        return (
            Feed(
                id="research",
                title="search in force",
                volatility=Volatility.live,
                entries=(
                    FeedEntry(
                        key="backend",
                        line=f"search backend: {label}",
                        trust=Trust.observed,
                        source="research",
                    ),
                ),
                trust=Trust.observed,
            ),
        )


GIT_BRANCH = "git rev-parse --abbrev-ref HEAD"


async def _git_branch(client: EnvironmentsClient, environment_id: str, cwd: str) -> str:
    try:
        ran = await client.run(environment_id, GIT_BRANCH, cwd=cwd or ".")
    except DownstreamError:
        return ""
    if ran.exit_code not in {0, None}:
        return ""
    branch = ran.output.strip().splitlines()[0].strip() if ran.output.strip() else ""
    if not branch or branch == "HEAD":
        return ""
    return branch


def _persona_entries(payload: Any) -> tuple[FeedEntry, ...]:
    entries: list[FeedEntry] = []
    card = field(payload, "persona")
    if isinstance(card, dict):
        facts = [f"name {value}" for value in (text(card, "display_name"),) if value]
        pronouns = text(card, "pronouns")
        summary = text(card, "summary")
        if pronouns:
            facts.append(f"pronouns {pronouns}")
        if summary:
            facts.append(summary)
        if facts:
            entries.append(
                FeedEntry(
                    key="card",
                    setting="identity",
                    line=_bounded("assistant identity: " + "; ".join(facts), reference="card"),
                    trust=Trust.stated,
                    source="owner",
                    asserted_by=PERSONA_SERVICE,
                    recorded_at=_timestamp(card.get("updated_at")),
                )
            )
    for index, item in enumerate(rows(payload, "fields"), start=1):
        if not isinstance(item, dict):
            continue
        key = text(item, "key")
        value = json.dumps(item.get("value"), ensure_ascii=False, separators=(",", ":"))
        entries.append(_persona_entry(f"field_{index}", "identity", f"{key}: {value}", item, key))
    for index, item in enumerate(rows(payload, "notes"), start=1):
        if not isinstance(item, dict):
            continue
        note_id = text(item, "note_id")
        entries.append(_persona_entry(f"note_{index}", "notes", text(item, "body"), item, note_id))
    return tuple(entries)


def _persona_entry(
    key: str, setting: str, body: str, item: dict[str, Any], reference: str
) -> FeedEntry:
    source = text(item, "source")
    return FeedEntry(
        key=key,
        setting=setting,
        line=_bounded(body, reference=reference),
        trust=_SOURCE_TRUST.get(source, Trust.untrusted),
        source=source,
        asserted_by=text(item, "asserted_by"),
        recorded_at=_timestamp(item.get("updated_at")),
    )


def _bounded(value: str, *, reference: str = "") -> str:
    compact = " ".join(value.split())
    suffix = f" [ref {reference}]" if reference else ""
    if len(compact) + len(suffix) <= MAX_LINE_CHARS:
        return compact + suffix
    shown = MAX_LINE_CHARS
    while True:
        notice = f" … [showing {shown} of {len(compact)} characters; ref {reference}]"
        adjusted = max(0, MAX_LINE_CHARS - len(notice))
        if adjusted == shown:
            return compact[:shown] + notice
        shown = adjusted


def _duration(milliseconds: int) -> str:
    seconds = max(0, milliseconds) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def _timestamp(value: Any) -> Any:
    from lucy_api.clients.transport import moment  # noqa: PLC0415 - keeps the adapter narrow

    return moment(value)


__all__ = [
    "GIT_BRANCH",
    "MusicFeeds",
    "PersonaFeeds",
    "ResearchFeeds",
    "UserFeeds",
    "WorkspaceFeeds",
]
