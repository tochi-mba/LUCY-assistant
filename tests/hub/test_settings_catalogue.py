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
    assert values["memory_write_policy"] == "ask_first"
    assert values["response_style"] == "natural"
    assert values["model"] == "anthropic:claude-opus-5"
    assert values["agent_max_depth"] == 3
    assert values["memory_retrieval_limit"] == 12
    assert knob("prompt_allow_unknown_feed_fields").agent is AgentAccess.NEVER
    assert knob("disabled_capabilities").on_unavailable is OnUnavailable.REFUSE
    assert knob("permission_mode").on_unavailable is OnUnavailable.USE_DEFAULT
    assert knob("approval_policy").on_unavailable is OnUnavailable.REFUSE
    assert knob("vision_enabled").on_unavailable is OnUnavailable.REFUSE


def test_context_reclamation_knobs_are_bounded_settings() -> None:
    window = knob("max_context_tokens")
    kept = knob("tool_results_kept")
    history = knob("history_turns_kept")
    assert window is not None
    assert kept is not None
    assert history is not None
    assert (window.default, window.minimum, window.maximum) == (200_000, 8_000, 1_000_000)
    assert (kept.default, kept.minimum, kept.maximum) == (3, 0, 50)
    assert (history.default, history.minimum, history.maximum) == (4, 0, 100)
    for key in ("reserve_percent", "warn_at_percent", "compaction_trigger_percent"):
        assert knob(key) is not None


def test_model_and_subagent_round_limits_are_bounded_settings() -> None:
    model = knob("max_llm_turns")
    child = knob("max_subagent_turns")
    assert model is not None
    assert child is not None
    assert (model.default, model.minimum, model.maximum) == (12, 1, 100)
    assert (child.default, child.minimum, child.maximum) == (8, 1, 50)
    for key in (
        "max_output_tokens_per_turn",
        "max_tool_result_tokens",
        "max_tool_calls_per_turn",
        "max_turn_seconds",
        "max_steps_per_plan",
        "agent_result_token_cap",
        "temperature",
        "vision_enabled",
    ):
        assert knob(key) is not None


def test_an_unknown_key_is_absent_rather_than_invented() -> None:
    assert knob("not_a_real_setting") is None
