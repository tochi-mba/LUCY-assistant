"""The lucy settings this hub actually consumes, as one value.

Settings-api is the store. This module is the only place a turn reads those numbers, so a
hard-coded 25,000 in the loop and a catalogue default of 25,000 cannot drift apart without
this file changing.

A settings outage that cannot confirm a refuse key (`disabled_capabilities`,
`approval_policy`) blocks the turn rather than guessing. Falling back to "nothing disabled"
would re-enable something the person turned off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from lucy_api.settings.catalogue import DEFAULT_MODEL

if TYPE_CHECKING:
    from collections.abc import Sequence

_SPEC = re.compile(r"^[a-z0-9][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9._-]*$")
"""A provider:model id. Invalid values fall back to empty rather than guessing a provider."""

REFUSE_KEYS = frozenset({"disabled_capabilities", "approval_policy"})
"""Keys whose default is permissive. Guessing either of them during an outage is the leak."""

ALWAYS_ON = frozenset({"help", "work"})
"""Capabilities a person cannot turn off. Without help the model cannot ask for the rest,
and without work it cannot see what it already started. Helpers are not here: "no helpers
in this conversation" is a thing a person may reasonably want."""


def _clamp(value: object, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(maximum, max(minimum, value))


def _text(value: object, default: str, *, allowed: frozenset[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        return default
    if allowed is not None and value not in allowed:
        return default
    return value


def _flag(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _optional_spec(value: object) -> str:
    """A second model id, or empty when none was named or the spelling is unusable."""
    if not isinstance(value, str) or not value:
        return ""
    return value if _SPEC.fullmatch(value) else ""


@dataclass(frozen=True, slots=True)
class TurnPolicy:
    """Every lucy knob one turn reads, already clamped.

    `blocks_turn` is the outage verdict, not a setting. Callers raise rather than running
    with a guessed floor.
    """

    decisions: bool = False
    decision_shadow_mode: bool = True
    decision_capabilities: bool = True
    decision_memory: bool = True
    decision_recovery: bool = False
    decision_claims: bool = True
    decision_timeout_ms: int = 1000
    decision_max_per_turn: int = 8
    model: str = DEFAULT_MODEL
    fallback_model: str = ""
    thinking: str = "medium"
    max_thinking_tokens: int = 0
    stream_thinking: bool = False
    log_message_content: bool = False
    temperature: float = 1.0
    response_style: str = "natural"
    vision_enabled: bool = True
    max_output_tokens: int = 8_000
    max_tool_result_tokens: int = 25_000
    max_tool_calls: int = 60
    max_turn_seconds: int = 0
    max_llm_turns: int = 12
    max_subagent_turns: int = 8
    max_steps: int = 20
    max_parallel: int = 4
    step_timeout_ms: int = 10_000
    plan_timeout_ms: int = 60_000
    render_read_tokens: int = 2_000
    render_preview_tokens: int = 400
    render_total_tokens: int = 8_000
    agent_max_depth: int = 3
    agent_max_concurrent: int = 5
    agent_result_token_cap: int = 2_000
    agent_wall_clock_seconds: int = 600
    agent_message_max_chars: int = 4_000
    agent_message_burst: int = 5
    retry_attempts: int = 2
    retry_max_seconds: int = 30
    downstream_timeout_seconds: int = 10
    auto_title: bool = True
    session_idle_archive_days: int = 30
    notify_on_long_turn: bool = True
    long_turn_seconds: int = 60
    confirm_outward_actions: bool = True
    memory_retrieval_limit: int = 12
    memory_write_policy: str = "ask_first"
    workspace_retention_hours: int = 24
    permission_mode: str = "ask"
    input_policy: str = "enqueue"
    incognito: bool = False
    approval_policy: str = "destructive_always_asks"
    max_context_tokens: int = 200_000
    reserve_percent: int = 13
    warn_at_percent: int = 60
    compaction_trigger_percent: int = 72
    history_turns_kept: int = 4
    tool_results_kept: int = 3
    session_token_budget: int = 0
    disabled: tuple[str, ...] = ()
    """What the profile turned off, from settings. The session's own list is kept apart so
    a change to one never has to be un-mixed from the other."""
    session_disabled: tuple[str, ...] = ()
    enabled: tuple[str, ...] = ()
    blocks_turn: bool = False

    @property
    def all_disabled(self) -> tuple[str, ...]:
        """The profile's list and the session's, as one, in that order."""
        return tuple(dict.fromkeys((*self.disabled, *self.session_disabled)))

    def for_session(self, disabled: Sequence[str]) -> TurnPolicy:
        """The same policy, with this one conversation's list in place of any earlier one.

        A session can only add to the profile's list, never remove from it: a person who
        turned music off for every conversation does not get it back in one of them by
        accident. The always-on pair stays on here for the same reason it does in settings.
        """
        names = tuple(dict.fromkeys(name for name in disabled if name not in ALWAYS_ON))
        return replace(self, session_disabled=names)

    @classmethod
    def from_resolved(cls, resolved: object | None) -> TurnPolicy:
        """Build from one namespace resolve, or refuse the turn when that cannot be trusted."""
        if resolved is None:
            return cls(blocks_turn=True)
        getter = getattr(resolved, "get", None)
        if not callable(getter):
            return cls(blocks_turn=True)
        refused = frozenset(getattr(resolved, "refused", ()))
        blocked = bool(refused & REFUSE_KEYS)

        def read(key: str, default: Any) -> Any:
            if key in refused:
                return default
            return getter(key, default)

        hundredths = _clamp(read("temperature", 100), 100, minimum=0, maximum=200)
        disabled = tuple(
            name for name in _names(read("disabled_capabilities", [])) if name not in ALWAYS_ON
        )
        return cls(
            decisions=_flag(read("decisions", False), False),
            decision_shadow_mode=_flag(read("decision_shadow_mode", True), True),
            decision_capabilities=_flag(read("decision_capabilities", True), True),
            decision_memory=_flag(read("decision_memory", True), True),
            decision_recovery=_flag(read("decision_recovery", False), False),
            decision_claims=_flag(read("decision_claims", True), True),
            decision_timeout_ms=_clamp(
                read("decision_timeout_ms", 1000), 1000, minimum=50, maximum=5000
            ),
            decision_max_per_turn=_clamp(
                read("decision_max_per_turn", 8), 8, minimum=1, maximum=32
            ),
            model=_text(read("model", DEFAULT_MODEL), DEFAULT_MODEL),
            fallback_model=_optional_spec(read("fallback_model", "")),
            thinking=_text(
                read("thinking", "medium"),
                "medium",
                allowed=frozenset({"off", "minimal", "low", "medium", "high"}),
            ),
            max_thinking_tokens=_clamp(
                read("max_thinking_tokens", 0), 0, minimum=0, maximum=200_000
            ),
            stream_thinking=_flag(read("stream_thinking", False), False),
            log_message_content=_flag(read("log_message_content", False), False),
            temperature=hundredths / 100.0,
            response_style=_text(
                read("response_style", "natural"),
                "natural",
                allowed=frozenset({"brief", "natural", "thorough"}),
            ),
            vision_enabled=_flag(read("vision_enabled", True), True),
            max_output_tokens=_clamp(
                read("max_output_tokens_per_turn", 8_000), 8_000, minimum=256, maximum=128_000
            ),
            max_tool_result_tokens=_clamp(
                read("max_tool_result_tokens", 25_000), 25_000, minimum=1_000, maximum=100_000
            ),
            max_tool_calls=_clamp(read("max_tool_calls_per_turn", 60), 60, minimum=1, maximum=500),
            max_turn_seconds=_clamp(read("max_turn_seconds", 0), 0, minimum=0, maximum=86_400),
            max_llm_turns=_clamp(read("max_llm_turns", 12), 12, minimum=1, maximum=100),
            max_subagent_turns=_clamp(read("max_subagent_turns", 8), 8, minimum=1, maximum=50),
            max_steps=_clamp(read("max_steps_per_plan", 20), 20, minimum=1, maximum=100),
            max_parallel=_clamp(read("max_parallel_steps", 4), 4, minimum=1, maximum=16),
            step_timeout_ms=_clamp(read("step_timeout_seconds", 10), 10, minimum=1, maximum=600)
            * 1_000,
            plan_timeout_ms=_clamp(read("plan_timeout_seconds", 60), 60, minimum=1, maximum=3_600)
            * 1_000,
            render_read_tokens=_clamp(
                read("render_read_tokens", 2_000), 2_000, minimum=100, maximum=20_000
            ),
            render_preview_tokens=_clamp(
                read("render_preview_tokens", 400), 400, minimum=50, maximum=5_000
            ),
            render_total_tokens=_clamp(
                read("render_total_tokens", 8_000), 8_000, minimum=500, maximum=100_000
            ),
            agent_max_depth=_clamp(read("agent_max_depth", 3), 3, minimum=1, maximum=5),
            agent_max_concurrent=_clamp(read("agent_max_concurrent", 5), 5, minimum=1, maximum=20),
            agent_result_token_cap=_clamp(
                read("agent_result_token_cap", 2_000), 2_000, minimum=200, maximum=20_000
            ),
            agent_wall_clock_seconds=_clamp(
                read("agent_wall_clock_seconds", 600), 600, minimum=10, maximum=7_200
            ),
            agent_message_max_chars=_clamp(
                read("agent_message_max_chars", 4_000), 4_000, minimum=100, maximum=32_000
            ),
            agent_message_burst=_clamp(read("agent_message_burst", 5), 5, minimum=1, maximum=50),
            retry_attempts=_clamp(read("retry_attempts", 2), 2, minimum=0, maximum=10),
            retry_max_seconds=_clamp(read("retry_max_seconds", 30), 30, minimum=1, maximum=600),
            downstream_timeout_seconds=_clamp(
                read("downstream_timeout_seconds", 10), 10, minimum=1, maximum=300
            ),
            auto_title=_flag(read("auto_title", True), True),
            session_idle_archive_days=_clamp(
                read("session_idle_archive_days", 30), 30, minimum=0, maximum=3_650
            ),
            notify_on_long_turn=_flag(read("notify_on_long_turn", True), True),
            long_turn_seconds=_clamp(read("long_turn_seconds", 60), 60, minimum=5, maximum=3_600),
            confirm_outward_actions=_flag(read("confirm_outward_actions", True), True),
            memory_retrieval_limit=_clamp(
                read("memory_retrieval_limit", 12), 12, minimum=0, maximum=100
            ),
            memory_write_policy=_text(
                read("memory_write_policy", "ask_first"),
                "ask_first",
                allowed=frozenset({"never", "ask_first", "automatic"}),
            ),
            workspace_retention_hours=_clamp(
                read("workspace_retention_hours", 24), 24, minimum=1, maximum=720
            ),
            permission_mode=_text(
                read("permission_mode", "ask"),
                "ask",
                allowed=frozenset({"ask", "accept_edits", "plan", "auto"}),
            ),
            input_policy=_text(
                read("input_policy", "enqueue"),
                "enqueue",
                allowed=frozenset({"reject", "enqueue", "interrupt", "rollback"}),
            ),
            incognito=_flag(read("incognito", False), False),
            max_context_tokens=_clamp(
                read("max_context_tokens", 200_000), 200_000, minimum=8_000, maximum=1_000_000
            ),
            reserve_percent=_clamp(read("reserve_percent", 13), 13, minimum=5, maximum=40),
            warn_at_percent=_clamp(read("warn_at_percent", 60), 60, minimum=10, maximum=95),
            compaction_trigger_percent=_clamp(
                read("compaction_trigger_percent", 72), 72, minimum=50, maximum=90
            ),
            history_turns_kept=_clamp(read("history_turns_kept", 4), 4, minimum=0, maximum=100),
            tool_results_kept=_clamp(read("tool_results_kept", 3), 3, minimum=0, maximum=50),
            session_token_budget=_clamp(
                read("session_token_budget", 0), 0, minimum=0, maximum=100_000_000
            ),
            approval_policy=_text(
                read("approval_policy", "destructive_always_asks"),
                "destructive_always_asks",
                allowed=frozenset({"destructive_always_asks", "spend_and_destructive_ask"}),
            ),
            disabled=disabled,
            enabled=_names(read("enabled_capabilities", [])),
            blocks_turn=blocked,
        )


SETTINGS_UNAVAILABLE = (
    "Settings could not be reached, and this turn's safety limits cannot be guessed. "
    "Try again in a moment."
)


__all__ = ["ALWAYS_ON", "REFUSE_KEYS", "SETTINGS_UNAVAILABLE", "TurnPolicy"]
