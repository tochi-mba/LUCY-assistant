"""Which feed lines survive, once the person's settings have had their say.

A sibling publishes everything it thinks is true. Lucy decides what the model is allowed to
see. The defaults are on for the lines that stop the model guessing, and off for the ones
that are nice-to-have or easy to leak (the next track, the last shell command, a search
backend). Unknown keys are dropped unless the person turned ``prompt_allow_unknown_feed_fields``
on, because a new key is how a compromised sibling would smuggle an instruction.
"""

from __future__ import annotations

from typing import Protocol

from lucy_api.context.feeds import CollectedFeeds, Feed, FeedEntry, Volatility
from lucy_api.context.fields import FIELDS, feed_setting_key, field_setting_key, known_keys

FIELD_DEFAULTS = {field.setting_key: field.default for field in FIELDS}

MASTER = "prompt_feeds_enabled"
HIDE_PERSONAL = "prompt_hide_personal_feeds"
ALLOW_UNKNOWN = "prompt_allow_unknown_feed_fields"


class Flags(Protocol):
    """The tiny surface policy needs: a boolean, with a default when the key was never set."""

    def flag(self, key: str, default: bool = True) -> bool: ...


class DefaultFlags:
    """Every toggle at its catalogue default. What an outage of settings-api lands on."""

    def flag(self, key: str, default: bool = True) -> bool:  # noqa: ARG002 - Flags
        return default


class ExplicitFlags:
    """A test double, and anything else that already resolved settings into a mapping."""

    def __init__(self, values: dict[str, bool] | None = None) -> None:
        self.values = values or {}

    def flag(self, key: str, default: bool = True) -> bool:
        return self.values.get(key, default)


def apply_policy(
    collected: CollectedFeeds,
    flags: Flags | None = None,
    *,
    incognito: bool = False,
) -> CollectedFeeds:
    """Strip whole feeds and individual keys the person (or incognito) turned off."""
    view = flags if flags is not None else DefaultFlags()
    if not view.flag(MASTER, True):
        return CollectedFeeds(failures=collected.failures)
    hide_personal = incognito and view.flag(HIDE_PERSONAL, True)
    allow_unknown = view.flag(ALLOW_UNKNOWN, False)
    kept: list[Feed] = []
    for feed in collected.all:
        if hide_personal and feed.personal:
            continue
        if not view.flag(feed_setting_key(feed.id), True):
            continue
        allowed = known_keys(feed.id)
        entries: list[FeedEntry] = []
        for entry in feed.entries:
            if entry.key not in allowed and not allow_unknown:
                continue
            if entry.key in allowed and not view.flag(
                field_setting_key(feed.id, entry.key),
                FIELD_DEFAULTS.get(field_setting_key(feed.id, entry.key), True),
            ):
                continue
            entries.append(entry)
        if entries:
            kept.append(feed.with_entries(tuple(entries)))
    standing = tuple(feed for feed in kept if feed.volatility is Volatility.standing)
    live = tuple(feed for feed in kept if feed.volatility is Volatility.live)
    return CollectedFeeds(standing=standing, live=live, failures=collected.failures)


__all__ = [
    "ALLOW_UNKNOWN",
    "HIDE_PERSONAL",
    "MASTER",
    "DefaultFlags",
    "ExplicitFlags",
    "Flags",
    "apply_policy",
]
