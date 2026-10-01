"""How a person asked to be written to: their time zone, language, units, clock and currency.

These five live in the `common` namespace, because every service would otherwise ask them
separately. Settings-api merges `common` underneath every namespace it resolves, so they
arrive with the `lucy` values and need no second round trip. Two of Lucy's own sit beside
them, because they answer the same question: `formatting` and `emoji`.

One of them changes what the model is told on every turn, and the rest only when the person
chose something:

- The time zone decides the clock on the live block's `now` line. "Remind me at nine" is a
  question about the person's nine, and a model shown only UTC answers with its own.
- A language, imperial units, the 12-hour clock, a currency, plain text or Markdown, and
  no emoji are stated in the prompt when they were chosen. Left alone they say nothing, and
  the model follows how the person writes, which is the better guide of the two until
  somebody has said otherwise.

A value that cannot be used (a zone the tz database does not have, a tag that is not a
language tag) is treated as not chosen. Settings-api validates every write, so this is the
second fence, not the first; it is here because what these become is text in a prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_ZONE = "UTC"

_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")
"""A BCP-47 tag as people write one: `fr`, `en-GB`, `zh-Hant-TW`."""

_CURRENCY = re.compile(r"^[A-Z]{3}$")
"""An ISO 4217 code."""

UNITS = frozenset({"metric", "imperial"})
CLOCKS = frozenset({"24h", "12h"})
LAYOUTS = frozenset({"auto", "plain", "markdown"})


def _zone_name(value: object) -> str:
    """An IANA name the tz database on this machine has, or the default."""
    if not isinstance(value, str) or not value or value == DEFAULT_ZONE:
        return DEFAULT_ZONE
    try:
        ZoneInfo(value)
    except (KeyError, ValueError, OSError):
        # Unknown names are a KeyError, malformed ones a ValueError, and a name that is a
        # directory of the database ("Europe") is an OSError on some platforms.
        return DEFAULT_ZONE
    return value


def _matching(value: object, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        return ""
    return value


def _one_of(value: object, allowed: frozenset[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


@dataclass(frozen=True, slots=True)
class Conventions:
    """One person's choices, already checked. The defaults are "nothing chosen"."""

    timezone: str = DEFAULT_ZONE
    locale: str = ""
    units: str = "metric"
    time_format: str = "24h"
    currency: str = ""
    formatting: str = "auto"
    emoji: bool = True

    @classmethod
    def from_reader(cls, read: Callable[[str, Any], Any]) -> Conventions:
        """Build from whatever reads one resolved key with a fallback."""
        return cls(
            timezone=_zone_name(read("timezone", DEFAULT_ZONE)),
            locale=_matching(read("locale", ""), _LANGUAGE_TAG),
            units=_one_of(read("units", "metric"), UNITS, "metric"),
            time_format=_one_of(read("time_format", "24h"), CLOCKS, "24h"),
            currency=_matching(read("currency", ""), _CURRENCY),
            formatting=_one_of(read("formatting", "auto"), LAYOUTS, "auto"),
            emoji=read("emoji", True) is not False,
        )

    def zone(self) -> tzinfo:
        """The zone the live block's clock is read in."""
        return UTC if self.timezone == DEFAULT_ZONE else ZoneInfo(self.timezone)

    def now(self) -> datetime:
        """This moment, on the person's clock."""
        return datetime.now(self.zone())

    def hint(self) -> str:
        """What the prompt says about how to write to this person. Empty when nothing was chosen."""
        chosen: list[str] = []
        if self.locale:
            chosen.append(
                f"Write to them in {self.locale}: its language, its spelling, and its date "
                "and number formats. If they write to you in another language, answer in "
                "that one."
            )
        if self.units == "imperial":
            chosen.append("Give distances, weights and temperatures in imperial units.")
        if self.time_format == "12h":
            chosen.append("Write clock times on the 12-hour clock, as 2:20 pm.")
        if self.currency:
            chosen.append(f"Give costs in {self.currency}.")
        if self.formatting == "plain":
            chosen.append(
                "Write plain text: no Markdown headings, lists, tables or emphasis marks. "
                "What they read you on shows text exactly as it arrives."
            )
        if self.formatting == "markdown":
            chosen.append(
                "What they read you on renders Markdown, so use headings, lists and tables "
                "where they make an answer easier to read."
            )
        if not self.emoji:
            chosen.append("Do not use emoji.")
        if not chosen:
            return ""
        return "This person chose how they are written to. " + " ".join(chosen)


NOTHING_CHOSEN = Conventions()
"""What a person who has set none of them has. Frozen, so one value serves everyone."""

__all__ = ["CLOCKS", "DEFAULT_ZONE", "LAYOUTS", "NOTHING_CHOSEN", "UNITS", "Conventions"]
