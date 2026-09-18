"""How Lucy shows settings: by capability, never by service.

Settings-api stores ``namespace.key``. The person and the model should never have to know
that. This module is the only place that mapping is allowed to live, so adding a sibling
adds a row here and the grouped list, the tool names, and the docs stay in lockstep.

Prompt-feed toggles are stored on the ``lucy`` namespace because Lucy decides what the
model sees. They are *shown* with the capability they describe: ``lucy.feeds_music_now_playing``
is a Music setting, not a Lucy-herself setting.
"""

from __future__ import annotations

from dataclasses import dataclass

from lucy_api.context.fields import FIELDS, capabilities, feed_setting_key


@dataclass(frozen=True, slots=True)
class SettingGroup:
    """One capability's worth of settings, as the person sees them."""

    id: str
    title: str
    namespaces: tuple[str, ...]
    summary: str


GROUPS: tuple[SettingGroup, ...] = (
    SettingGroup(
        "lucy",
        "Lucy",
        ("lucy", "common"),
        "The model, permissions, prompt placement, and answers every service needs.",
    ),
    SettingGroup(
        "account",
        "Account",
        ("user", "keyring"),
        "Who you are on this box, how long a session lasts, deletion, and which pinned "
        "facts the model may see.",
    ),
    SettingGroup(
        "persona",
        "Persona",
        ("persona",),
        "Which persona loads, how much of it is always in the prompt, and how it is forgotten.",
    ),
    SettingGroup(
        "memory",
        "Memory",
        ("memory",),
        "What Lucy keeps about you, how much it may retrieve, and how long forgotten rows last.",
    ),
    SettingGroup(
        "music",
        "Music",
        ("spotify",),
        "Playback defaults, and which now-playing lines the model is allowed to see.",
    ),
    SettingGroup(
        "research",
        "Research",
        ("search",),
        "Search providers, result count, and whether the live block names the backend.",
    ),
    SettingGroup(
        "workspace",
        "Workspace",
        ("environments",),
        "How long a sandbox lives, command limits, and which shell facts the model sees.",
    ),
)

NAMESPACE_TO_GROUP: dict[str, str] = {
    namespace: group.id for group in GROUPS for namespace in group.namespaces
}

FEED_PREFIX_TO_GROUP: dict[str, str] = {name: name for name in capabilities()}

PUBLIC_SERVICE_NAMES = frozenset(
    {
        "spotify",
        "spotify-api",
        "settings-api",
        "environments-api",
        "web-search-api",
        "persona-api",
        "memory-api",
        "user-api",
        "keyring",
        "keyring-api",
    }
)
"""The names a prompt must never contain, for the services this build knows about.

Public names only. A private service's name is not written down here, because writing it
down is the leak -- a denylist that names the thing it is hiding has published it. Private
names are read from the local manifest at runtime instead; see `service_names`. ADR-0011.
"""


def service_names() -> frozenset[str]:
    """Every name a prompt must not contain, including privately installed ones.

    The local manifest is gitignored and usually absent, in which case this is exactly the
    public set. When it is present its folder names are added, so a private service is
    covered by the same check without ever appearing in a public file.
    """
    from pathlib import Path  # noqa: PLC0415 - one import, one use

    private: set[str] = set()
    manifest = Path.cwd() / ".repos.local.txt"
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].split()
            if entry:
                private.add(entry[0].lower())
    return PUBLIC_SERVICE_NAMES | private


SERVICE_NAMES = PUBLIC_SERVICE_NAMES
"""Kept for callers that want the static set. Prefer `service_names()`."""


def group_for(qualified: str) -> str:
    """The capability id a ``namespace.key`` belongs in when Lucy shows it."""
    namespace, _, key = qualified.partition(".")
    if namespace == "lucy" and key.startswith("feeds_"):
        capability = key.removeprefix("feeds_").split("_", 1)[0]
        return FEED_PREFIX_TO_GROUP.get(capability, "lucy")
    # A namespace with no group is one this build does not ship -- a privately installed
    # capability, or one added by a sibling before Lucy learned about it. It is shown under
    # its own name rather than raising: a setting a person can see and cannot place is a
    # smaller problem than a settings page that will not render. See ADR-0011.
    return NAMESPACE_TO_GROUP.get(namespace, namespace)


def feed_keys_for(capability: str) -> tuple[str, ...]:
    """Qualified lucy keys that belong with this capability's other settings."""
    keys = [f"lucy.{feed_setting_key(capability)}"]
    keys.extend(f"lucy.{field.setting_key}" for field in FIELDS if field.capability == capability)
    return tuple(keys)


def group(capability: str) -> SettingGroup:
    return next(item for item in GROUPS if item.id == capability)


__all__ = [
    "FEED_PREFIX_TO_GROUP",
    "GROUPS",
    "NAMESPACE_TO_GROUP",
    "SERVICE_NAMES",
    "SettingGroup",
    "feed_keys_for",
    "group",
    "group_for",
    "service_names",
]
