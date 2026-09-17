"""The lucy namespace is complete, conservative, and named the way settings-api allows."""

from __future__ import annotations

import re

from lucy_api.context.fields import FIELDS, capabilities, feed_setting_key
from lucy_api.settings.catalogue import (
    KNOBS,
    AgentAccess,
    OnUnavailable,
    ValueType,
    defaults,
    knob,
)

KEY = re.compile(r"^[a-z][a-z0-9_]*$")


def test_every_key_is_unique_and_legal() -> None:
    keys = [item.key for item in KNOBS]
    assert len(keys) == len(set(keys))
    assert all(KEY.match(key) for key in keys)


def test_every_declared_feed_field_has_a_toggle_and_a_capability_switch() -> None:
    for capability in capabilities():
        assert knob(feed_setting_key(capability)) is not None
    for field in FIELDS:
        item = knob(field.setting_key)
        assert item is not None
        assert item.default is field.default
        assert item.value_type is ValueType.BOOL


def test_privacy_sensitive_defaults_are_the_conservative_ones() -> None:
    values = defaults()
    assert values["log_message_content"] is False
    assert values["stream_thinking"] is False
    assert values["prompt_allow_unknown_feed_fields"] is False
    assert values["prompt_hide_personal_feeds"] is True
    assert values["feeds_music_queue_head"] is False
    assert values["feeds_workspace_last_command"] is False
    assert values["memory_write_policy"] == "ask"
    assert knob("prompt_allow_unknown_feed_fields").agent is AgentAccess.NEVER
    assert knob("disabled_capabilities").on_unavailable is OnUnavailable.REFUSE
    assert knob("permission_mode").on_unavailable is OnUnavailable.REFUSE
    assert knob("approval_policy").on_unavailable is OnUnavailable.REFUSE


def test_an_unknown_key_is_absent_rather_than_invented() -> None:
    assert knob("not_a_real_setting") is None
