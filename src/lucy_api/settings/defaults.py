"""Sibling settings a capability uses when the model left a field out. Never secrets.

The person chose a search backend, a playback device, an owner for new repositories; a model
that omits the field should get the person's choice, not the service's. Each is read from the
sibling's own namespace -- the service owns the setting, the hub only reads it -- and put on
`PackContext.defaults` under the capability's name (`music.device_id`, `repos.owner`), where the
pack that owns the field looks. One function, so a new capability's defaults are one more arm
here rather than another dozen lines in the composition root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

SEARCH_NAMESPACE = "search"
"""web-search's namespace, read for the person's backend and result count."""

MUSIC_NAMESPACE = "spotify"
"""spotify's namespace, read for the person's default playback device."""

REPOS_NAMESPACE = "github"
"""Github-api's namespace, read for where new repositories go and how they are seen."""

VISIBILITIES = frozenset({"private", "public", "internal"})


class Resolved(Protocol):
    """What a resolved namespace offers: one value by key, with a fallback."""

    def get(self, key: str, default: object = None) -> object: ...


def pack_defaults(resolved: Mapping[str, Resolved | None]) -> dict[str, object]:
    """Every default the resolved namespaces supply. A namespace that is ``None`` supplies none."""
    defaults: dict[str, object] = {}
    search = resolved.get(SEARCH_NAMESPACE)
    if search is not None:
        limit = search.get("default_result_count", 8)
        if isinstance(limit, int) and not isinstance(limit, bool):
            defaults["research.limit"] = min(20, max(1, limit))
        backend = search.get("search_backend", "google")
        if isinstance(backend, str) and backend:
            defaults["research.backend"] = backend
    music = resolved.get(MUSIC_NAMESPACE)
    if music is not None:
        device = music.get("default_device", None)
        if isinstance(device, str) and device:
            defaults["music.device_id"] = device
    repos = resolved.get(REPOS_NAMESPACE)
    if repos is not None:
        owner = repos.get("default_owner", "")
        if isinstance(owner, str) and owner:
            defaults["repos.owner"] = owner
        visibility = repos.get("default_visibility", None)
        if isinstance(visibility, str) and visibility in VISIBILITIES:
            defaults["repos.visibility"] = visibility
    return defaults


__all__ = [
    "MUSIC_NAMESPACE",
    "REPOS_NAMESPACE",
    "SEARCH_NAMESPACE",
    "VISIBILITIES",
    "pack_defaults",
]
