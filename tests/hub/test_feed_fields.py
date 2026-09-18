"""Extension feed fields are discovered; a built-in capability cannot be redefined."""

from __future__ import annotations

from typing import Any

import pytest

from lucy_api.context.feeds import Volatility
from lucy_api.context.fields import (
    BUILTIN_FIELDS,
    EXTENSION_GROUP,
    FeedField,
    _discovered,
    capabilities,
    feed_setting_key,
    field_setting_key,
    fields_for,
    known_keys,
)


class _Entry:
    def __init__(self, name: str, payload: Any) -> None:
        self.name = name
        self._payload = payload

    def load(self) -> Any:
        return self._payload


def test_a_field_knows_both_its_per_line_and_its_capability_setting_keys() -> None:
    field = FeedField("archive", "status", Volatility.live, "Whether the archive is running.")
    assert field.setting_key == "feeds_archive_status"
    assert field.feed_setting_key == "feeds_archive"
    assert feed_setting_key("archive") == "feeds_archive"
    assert field_setting_key("archive", "status") == "feeds_archive_status"


def test_an_extension_can_add_fields_for_a_capability_this_build_does_not_ship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extra = FeedField("archive", "status", Volatility.live, "Whether it is running.")

    def entry_points(*, group: str = "") -> list[_Entry]:
        if group != EXTENSION_GROUP:
            return []
        return [_Entry("archive", [extra])]

    monkeypatch.setattr("importlib.metadata.entry_points", entry_points)
    found = _discovered()
    assert found == (extra,)
    assert "persona" in capabilities()
    assert "identity" in known_keys("persona")
    assert fields_for("persona")[0] in BUILTIN_FIELDS


def test_an_extension_that_does_not_return_fields_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda *, group="": [_Entry("archive", ["not-a-field"])],
    )
    with pytest.raises(TypeError, match="FeedField"):
        _discovered()


def test_an_extension_cannot_redefine_a_built_in_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clash = FeedField("music", "now_playing", Volatility.live, "A colliding line.")
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda *, group="": [_Entry("music-extra", [clash])],
    )
    with pytest.raises(ValueError, match="already owns"):
        _discovered()
