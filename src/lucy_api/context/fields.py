"""The keyed facts a capability may put in front of the model.

A sibling can send whatever keys it likes; Lucy only *keeps* the ones declared here, unless
the person explicitly allows unknown fields. That is the difference between "the music
service said what is playing" and "the music service invented a new instruction". Each
row is also a setting: ``feeds_music`` for the whole capability, ``feeds_music_now_playing``
for one line.

Nothing here is a service name. The model reads ``music`` and ``workspace``, never the
process that produced the line.
"""

from __future__ import annotations

from dataclasses import dataclass

from lucy_api.context.feeds import Volatility

SETTING_KEY = r"^[a-z][a-z0-9_]*$"
"""The same shape settings-api accepts, so these keys can be registered without renaming."""


@dataclass(frozen=True, slots=True)
class FeedField:
    """One toggleable line a capability is allowed to contribute."""

    capability: str
    key: str
    volatility: Volatility
    summary: str
    default: bool = True
    personal: bool = True

    @property
    def setting_key(self) -> str:
        return f"feeds_{self.capability}_{self.key}"

    @property
    def feed_setting_key(self) -> str:
        return f"feeds_{self.capability}"


# Every field Lucy is willing to show. Adding a row here is what makes a new line both
# renderable and togglable; a sibling sending a key that is not on this list is dropped.
BUILTIN_FIELDS: tuple[FeedField, ...] = (
    # Persona: who Lucy is for this person, and the notes that person has pinned. Standing,
    # because a correction should break the cached prefix and nothing else should.
    FeedField("persona", "identity", Volatility.standing, "Who you are, in this profile."),
    FeedField("persona", "notes", Volatility.standing, "Pinned notes about the person."),
    # Music: facts that change while a song plays. Live, because they are stale by next turn.
    FeedField("music", "now_playing", Volatility.live, "What is playing, and how far in."),
    FeedField("music", "device", Volatility.live, "The speaker or computer audio is coming from."),
    FeedField("music", "shuffled", Volatility.live, "Whether the queue is shuffled."),
    FeedField("music", "repeat", Volatility.live, "Whether the queue or track repeats."),
    FeedField("music", "queue_head", Volatility.live, "What is lined up next.", default=False),
    # Workspace: the running environment, never the host path.
    FeedField("workspace", "cwd", Volatility.live, "The current directory inside the workspace."),
    FeedField("workspace", "shell", Volatility.live, "Which shell is running."),
    FeedField("workspace", "pid", Volatility.live, "The running shell's process id."),
    FeedField(
        "workspace", "shells_running", Volatility.live, "How many shells are open right now."
    ),
    FeedField("workspace", "sandbox", Volatility.live, "How isolated the workspace is."),
    FeedField(
        "workspace", "git_branch", Volatility.live, "The current git branch, if there is one."
    ),
    FeedField(
        "workspace",
        "last_command",
        Volatility.live,
        "The last command that ran, without its output.",
        default=False,
    ),
    # Research: only the backend in force. Query text is history, not live state.
    FeedField(
        "research",
        "backend",
        Volatility.live,
        "Which search backend is in force, so the model does not retry the other one.",
        default=False,
        personal=False,
    ),
)


EXTENSION_GROUP = "lucy.feeds"
"""Where a capability this build does not ship declares its own fields.

The family is public and some of its members are not. A private capability registers a
sequence of `FeedField` under this entry-point group and its lines become toggleable in
exactly the same way as a built-in one -- without the public tree containing its name, its
capabilities, or a hint that it exists. See ADR-0011.

Discovery happens once, at import, so a malformed extension fails at startup rather than
mid-turn. An extension that declares a capability a built-in already owns is refused: two
tables disagreeing about what `music` may show is worse than either of them alone.
"""


def _discovered() -> tuple[FeedField, ...]:
    """Fields contributed by installed extensions, validated against the built-ins."""
    from importlib.metadata import entry_points  # noqa: PLC0415 - one import, one use

    builtin = {field.capability for field in BUILTIN_FIELDS}
    found: list[FeedField] = []
    for entry in entry_points(group=EXTENSION_GROUP):
        for field in entry.load():
            if not isinstance(field, FeedField):
                message = f"{entry.name} contributed {type(field).__name__}, not a FeedField"
                raise TypeError(message)
            if field.capability in builtin:
                message = (
                    f"{entry.name} declares fields for {field.capability!r}, which this "
                    "build already owns; an extension may add a capability, never redefine one"
                )
                raise ValueError(message)
            found.append(field)
    return tuple(found)


FIELDS: tuple[FeedField, ...] = (*BUILTIN_FIELDS, *_discovered())
"""Everything the model may be shown: what this build ships, plus what is installed."""


def fields_for(capability: str) -> tuple[FeedField, ...]:
    return tuple(field for field in FIELDS if field.capability == capability)


def known_keys(capability: str) -> frozenset[str]:
    return frozenset(field.key for field in fields_for(capability))


def capabilities() -> tuple[str, ...]:
    seen: list[str] = []
    for field in FIELDS:
        if field.capability not in seen:
            seen.append(field.capability)
    return tuple(seen)


def feed_setting_key(capability: str) -> str:
    return f"feeds_{capability}"


def field_setting_key(capability: str, key: str) -> str:
    return f"feeds_{capability}_{key}"


__all__ = [
    "FIELDS",
    "FeedField",
    "capabilities",
    "feed_setting_key",
    "field_setting_key",
    "fields_for",
    "known_keys",
]
