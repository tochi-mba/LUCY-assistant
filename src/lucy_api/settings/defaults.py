"""Sibling settings a capability uses when the model left a field out. Never secrets.

The person chose a search backend, a playback device, an owner for new repositories, how a
pull request is merged, how long
a command may run, how many memories a recall brings back; a model that omits the field
should get the person's choice, not the service's. Each is read from the
sibling's own namespace -- the service owns the setting, the hub only reads it -- and put on
`PackContext.defaults` under the capability's name (`music.device_id`, `repos.owner`), where the
pack that owns the field looks. One function, so a new capability's defaults are one more arm
here rather than another dozen lines in the composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from settings_client import SettingsRefused

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SEARCH_NAMESPACE = "search"
"""web-search's namespace, read for the person's backend and result count."""

MUSIC_NAMESPACE = "spotify"
"""spotify's namespace, read for the person's default playback device."""

REPOS_NAMESPACE = "github"
"""Github-api's namespace, read for where new repositories go and how they are seen, and for
how the person merges, opens pull requests and watches them."""

VISIBILITIES = frozenset({"private", "public", "internal"})

MERGE_METHODS = frozenset({"merge", "squash", "rebase"})

METHOD_UNKNOWN = "unknown"
"""The merge method could not be read and must not be guessed: the catalogue refuses rather
than falls back, because squashing on a repository whose owner merges or rebases rewrites how
their history lands, and that cannot be cleanly undone. A merge that names no method under
this is refused and says why; one that names a method goes ahead."""

MAX_WATCH_HOURS = 168
"""The longest a repository watch lasts when the model does not say: a week, as the pack
holds a watch the model sized."""

WORKSPACE_NAMESPACE = "environments"
"""environments-api's namespace, read for how long a command may run and how much of its
output is kept when the model does not say."""

MEMORY_NAMESPACE = "memory"
"""memory-api's namespace, read for how many memories a recall brings back and how far down
the trust ladder it may reach."""

TRUST_FLOORS = ("stated", "observed", "inferred")
"""Most trusted first. A floor admits itself and everything before it."""

FLOOR_UNKNOWN = "unknown"
"""The floor could not be read and must not be guessed: the catalogue refuses rather than
falls back, because landing above what the person chose uses guesses they did not agree
to. A recall under this floor brings back nothing and says why."""

MAX_RECALL = 20
"""The most one recall may bring back, whatever the setting says."""


class Resolved(Protocol):
    """What a resolved namespace offers: one value by key, with a fallback."""

    def get(self, key: str, default: object = None) -> object: ...


def pack_defaults(resolved: Mapping[str, Resolved | None]) -> dict[str, object]:
    """Every default the resolved namespaces supply.

    A namespace that is ``None`` could not be read. It supplies nothing a pack may use, only
    the settings that refuse rather than fall back, each marked as not known.
    """
    defaults: dict[str, object] = {}
    for namespace, supply in _SUPPLIERS:
        values = resolved.get(namespace)
        if values is not None:
            defaults.update(supply(values))
        elif namespace in resolved:
            defaults.update(_UNREADABLE.get(namespace, {}))
    return defaults


def _research(search: Resolved) -> dict[str, object]:
    defaults: dict[str, object] = {}
    limit = _whole(search.get("default_result_count", 8))
    if limit is not None:
        defaults["research.limit"] = min(20, max(1, limit))
    backend = search.get("search_backend", "google")
    if isinstance(backend, str) and backend:
        defaults["research.backend"] = backend
    return defaults


def _music(music: Resolved) -> dict[str, object]:
    device = music.get("default_device", None)
    return {"music.device_id": device} if isinstance(device, str) and device else {}


def _repos(repos: Resolved) -> dict[str, object]:
    defaults: dict[str, object] = {}
    owner = repos.get("default_owner", "")
    if isinstance(owner, str) and owner:
        defaults["repos.owner"] = owner
    visibility = repos.get("default_visibility", None)
    if isinstance(visibility, str) and visibility in VISIBILITIES:
        defaults["repos.visibility"] = visibility
    method = _merge_method(repos)
    if method is not None:
        defaults["repos.merge_method"] = method
    draft = repos.get("draft_pull_requests", None)
    if isinstance(draft, bool):
        defaults["repos.draft"] = draft
    delete_branch = repos.get("delete_branch_after_merge", None)
    if isinstance(delete_branch, bool):
        defaults["repos.delete_branch"] = delete_branch
    hours = _whole(repos.get("watch_default_hours", None))
    if hours is not None:
        defaults["repos.watch_seconds"] = min(MAX_WATCH_HOURS, max(1, hours)) * 3600.0
    return defaults


def _workspace(workspace: Resolved) -> dict[str, object]:
    defaults: dict[str, object] = {}
    seconds = _whole(workspace.get("command_timeout_seconds", None))
    if seconds is not None:
        defaults["workspace.timeout_ms"] = max(1, seconds) * 1_000
    kept = _whole(workspace.get("max_output_bytes", None))
    if kept is not None:
        defaults["workspace.output_bytes"] = max(1, kept)
    return defaults


def _notes(memory: Resolved) -> dict[str, object]:
    defaults: dict[str, object] = {"notes.trust_floor": _trust_floor(memory)}
    limit = _whole(memory.get("retrieval_limit", None))
    if limit is not None:
        defaults["notes.limit"] = min(MAX_RECALL, max(0, limit))
    return defaults


_SUPPLIERS: tuple[tuple[str, Callable[[Resolved], dict[str, object]]], ...] = (
    (SEARCH_NAMESPACE, _research),
    (MUSIC_NAMESPACE, _music),
    (REPOS_NAMESPACE, _repos),
    (WORKSPACE_NAMESPACE, _workspace),
    (MEMORY_NAMESPACE, _notes),
)
"""One arm per namespace: what that sibling's settings supply, under the capability's name."""

_UNREADABLE: dict[str, dict[str, object]] = {
    MEMORY_NAMESPACE: {"notes.trust_floor": FLOOR_UNKNOWN},
    REPOS_NAMESPACE: {"repos.merge_method": METHOD_UNKNOWN},
}
"""What a namespace that could not be read still says: which refusing settings are not known."""


def _whole(value: object) -> int | None:
    """A whole number, or nothing. A flag is not a number, whatever Python says."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _trust_floor(memory: Resolved) -> str:
    """How far down the trust ladder a recall may reach, or that it is not known."""
    try:
        floor = memory.get("retrieval_trust_floor", TRUST_FLOORS[-1])
    except SettingsRefused:
        return FLOOR_UNKNOWN
    return floor if isinstance(floor, str) and floor in TRUST_FLOORS else FLOOR_UNKNOWN


def _merge_method(repos: Resolved) -> str | None:
    """How a merge lands when the model does not say: nothing chosen, a method, or not known."""
    try:
        method = repos.get("merge_method", None)
    except SettingsRefused:
        return METHOD_UNKNOWN
    if method is None:
        return None
    return method if isinstance(method, str) and method in MERGE_METHODS else METHOD_UNKNOWN


__all__ = [
    "FLOOR_UNKNOWN",
    "MAX_RECALL",
    "MAX_WATCH_HOURS",
    "MEMORY_NAMESPACE",
    "MERGE_METHODS",
    "METHOD_UNKNOWN",
    "MUSIC_NAMESPACE",
    "REPOS_NAMESPACE",
    "SEARCH_NAMESPACE",
    "TRUST_FLOORS",
    "VISIBILITIES",
    "WORKSPACE_NAMESPACE",
    "pack_defaults",
]
