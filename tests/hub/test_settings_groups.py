"""Settings are grouped by capability, including Lucy-owned feed toggles."""

from __future__ import annotations

from lucy_api.context.fields import FIELDS, capabilities
from lucy_api.settings.groups import (
    GROUPS,
    SERVICE_NAMES,
    feed_keys_for,
    group,
    group_for,
)


def test_every_capability_group_has_a_product_name_not_a_service_name() -> None:
    assert tuple(item.id for item in GROUPS) == (
        "lucy",
        "account",
        "persona",
        "memory",
        "music",
        "research",
        "workspace",
    )
    for item in GROUPS:
        assert item.id not in SERVICE_NAMES
        assert item.title.lower() not in SERVICE_NAMES
        for namespace in item.namespaces:
            assert " " not in namespace


def test_a_feed_toggle_is_shown_with_the_capability_it_describes() -> None:
    assert group_for("lucy.feeds_music_now_playing") == "music"
    assert group_for("lucy.feeds_workspace_last_command") == "workspace"
    assert group_for("lucy.feeds_persona_notes") == "persona"
    # A toggle for a capability this build does not ship falls back to `lucy` rather than
    # inventing a group. A privately installed capability registers its own fields and
    # brings its own group with it; see ADR-0011.
    assert group_for("lucy.feeds_unshipped_thing") == "lucy"
    assert group_for("lucy.feeds_research_backend") == "research"
    assert group_for("lucy.prompt_feeds_enabled") == "lucy"
    assert group_for("lucy.model") == "lucy"
    assert group_for("common.timezone") == "lucy"
    assert group_for("spotify.default_device") == "music"
    assert group_for("environments.command_timeout_seconds") == "workspace"
    assert group_for("search.default_result_count") == "research"
    # A namespace this build does not ship is shown under its own name rather than
    # bringing the page down.
    assert group_for("somethingelse.a_setting") == "somethingelse"
    assert group_for("user.erasure_mode") == "account"
    assert group_for("keyring.session_ttl_days") == "account"


def test_every_declared_feed_field_has_a_grouped_lucy_key() -> None:
    for capability in capabilities():
        keys = feed_keys_for(capability)
        assert f"lucy.feeds_{capability}" in keys
        for field in FIELDS:
            if field.capability == capability:
                assert f"lucy.{field.setting_key}" in keys
                assert group_for(f"lucy.{field.setting_key}") == capability


def test_group_lookup_is_the_row_in_groups() -> None:
    assert group("music") is GROUPS[4]
    assert group("music").namespaces == ("spotify",)
