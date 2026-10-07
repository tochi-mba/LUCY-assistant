"""Quiet hours: when Lucy may not open a turn on her own.

"Watch the deploy and tell me" should not mean a turn, a webhook and a phone buzz at three in
the morning. A person may name a window on their own clock (`lucy.quiet_hours`, read in
`common.timezone`), and an ending that would wake the session inside it is told the way
every ending is -- the `lucy.work.finished` event at once, the line in the next turn's live
block -- while the turn that tells them is opened when the window closes.

The window is captured when the work is opened, as a tag on its record and, for a
subscription, on its row. The waker runs with nobody present and no token to read settings
with, so it is handed the window rather than asked to look it up; a restart keeps it.

`23:00-07:00` wraps midnight, which is the usual case. A window whose start and end are the
same minute is no window: "quiet all day" would be a promise never to wake, and that is
`lucy.wake_by_default` turned off, not quiet hours.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

QUIET_TAG = "quiet"
"""The record tag that carries a window, as `23:00-07:00@Europe/London`."""

WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
"""`HH:MM-HH:MM` on the 24-hour clock, the shape settings-api validates `quiet_hours` to."""

NEAR_SECONDS = 60.0
"""How close to its end a window may be and still hold a wake back. Closer than this the
wake is opened now: a minute early is no intrusion, and a deferral that short is a timer
racing the clock it was set from."""


def _zone(name: str) -> tzinfo:
    return UTC if name == "UTC" else ZoneInfo(name)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True, slots=True)
class QuietHours:
    """One person's window, in minutes after their midnight, and the zone it is read in."""

    start: int
    end: int
    zone: str

    @classmethod
    def of(cls, window: str, zone: str) -> QuietHours | None:
        """The window, or ``None`` when it is empty, malformed, or names no usable zone."""
        found = WINDOW.fullmatch(window)
        if found is None:
            return None
        hour, minute, end_hour, end_minute = (int(part) for part in found.groups())
        start, end = hour * 60 + minute, end_hour * 60 + end_minute
        if start == end:
            return None
        try:
            _zone(zone)
        except (KeyError, ValueError, OSError):
            # The same three ways `settings.conventions` names: unknown, malformed, a folder.
            return None
        return cls(start, end, zone)

    @classmethod
    def from_tag(cls, tag: str) -> QuietHours | None:
        """The window a record carries, or ``None`` when it carries none."""
        window, _, zone = tag.partition("@")
        return cls.of(window, zone)

    @property
    def window(self) -> str:
        return f"{_hhmm(self.start)}-{_hhmm(self.end)}"

    def tag(self) -> str:
        return f"{self.window}@{self.zone}"

    def ends_at(self, stamp: float) -> float | None:
        """When the window `stamp` falls in closes, or ``None`` when it falls outside one.

        The end is read on the person's wall clock on the right day -- tomorrow's, for a
        window that wrapped midnight and was entered before it -- so a change of offset in
        between moves the moment with the clock, as the person would expect.
        """
        local = datetime.fromtimestamp(stamp, _zone(self.zone))
        minute = local.hour * 60 + local.minute
        if self.start < self.end:
            inside = self.start <= minute < self.end
        else:
            inside = minute >= self.start or minute < self.end
        if not inside:
            return None
        closes = local.replace(hour=self.end // 60, minute=self.end % 60, second=0, microsecond=0)
        if closes <= local:
            closes += timedelta(days=1)
        return closes.timestamp()

    def advice(self) -> str:
        """The sentence a tool result adds, so the model does not promise a 3am message."""
        return (
            f" They keep quiet hours ({self.window}, {self.zone}): an ending inside them is "
            f"told at {_hhmm(self.end)}, not when it happens."
        )


def quiet_tags(quiet: QuietHours | None, *, wake: bool) -> dict[str, str]:
    """The tag that holds a waking ending back for quiet hours, or none.

    Every kind of work that may wake a conversation carries it, or an ending at 3am wakes
    the person at 3am: watches and check-ins did, commands and helpers did not.
    """
    return {QUIET_TAG: quiet.tag()} if wake and quiet is not None else {}


__all__ = ["NEAR_SECONDS", "QUIET_TAG", "WINDOW", "QuietHours", "quiet_tags"]
