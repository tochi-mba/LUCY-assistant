"""The ``lucy`` settings namespace, as Lucy itself understands it.

Settings-api is the store. This module is the catalogue Lucy will register there: every
knob a person (or, with approval, the model) can turn, typed the way settings-api types
them, with a default that is safe to land on during an outage.

Keys match ``^[a-z][a-z0-9_]*$``. Dots are not allowed in a key, so a nested idea becomes
an underscore: ``feeds_music_now_playing``, not ``feeds.music.now_playing``. The prompt-feed
fields in :mod:`lucy_api.context.fields` generate their own rows so a new line on the model
cannot ship without a toggle that turns it off.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from lucy_api.context.fields import FIELDS, capabilities, feed_setting_key


class ValueType(StrEnum):
    BOOL = "bool"
    INT = "int"
    STR = "str"
    ENUM = "enum"
    STR_LIST = "str_list"


class OnUnavailable(StrEnum):
    USE_DEFAULT = "use_default"
    REFUSE = "refuse"


class AgentAccess(StrEnum):
    NEVER = "never"
    WITH_APPROVAL = "with_approval"
    FREELY = "freely"


@dataclass(frozen=True, slots=True)
class Knob:
    """One setting in the lucy namespace."""

    key: str
    summary: str
    value_type: ValueType
    default: bool | int | str | tuple[str, ...]
    on_unavailable: OnUnavailable
    description: str
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    agent: AgentAccess = AgentAccess.WITH_APPROVAL


def _bool(  # noqa: PLR0913
    key: str,
    default: bool,
    summary: str,
    description: str,
    *,
    unavailable: OnUnavailable,
    agent: AgentAccess = AgentAccess.WITH_APPROVAL,
) -> Knob:
    return Knob(
        key=key,
        summary=summary,
        value_type=ValueType.BOOL,
        default=default,
        on_unavailable=unavailable,
        description=description,
        agent=agent,
    )


def _int(  # noqa: PLR0913
    key: str,
    default: int,
    summary: str,
    description: str,
    *,
    minimum: int,
    maximum: int,
    unavailable: OnUnavailable,
) -> Knob:
    return Knob(
        key=key,
        summary=summary,
        value_type=ValueType.INT,
        default=default,
        on_unavailable=unavailable,
        description=description,
        minimum=minimum,
        maximum=maximum,
    )


def _enum(  # noqa: PLR0913
    key: str,
    default: str,
    choices: tuple[str, ...],
    summary: str,
    description: str,
    *,
    unavailable: OnUnavailable,
) -> Knob:
    return Knob(
        key=key,
        summary=summary,
        value_type=ValueType.ENUM,
        default=default,
        on_unavailable=unavailable,
        description=description,
        choices=choices,
        agent=AgentAccess.WITH_APPROVAL,
    )


def _core() -> tuple[Knob, ...]:
    return (
        Knob(
            key="model",
            summary="Which model Lucy uses for this profile.",
            value_type=ValueType.STR,
            default="anthropic:claude-opus-5",
            on_unavailable=OnUnavailable.USE_DEFAULT,
            description=(
                "Empty means the hub default. A named model must be one this deployment "
                "has credentials for."
            ),
        ),
        Knob(
            key="fallback_model",
            summary="Which model to try when the chosen one is unavailable.",
            value_type=ValueType.STR,
            default="",
            on_unavailable=OnUnavailable.USE_DEFAULT,
            description=(
                "Empty means there is no second choice: the turn fails and says so. "
                "Naming one makes an outage a reply in a different voice. Lucy says "
                "which model answered whenever it is not the one you chose."
            ),
        ),
        _enum(
            "thinking",
            "medium",
            ("off", "minimal", "low", "medium", "high"),
            "How much working-out the model is asked to do.",
            "Off skips it. High spends tokens on hard plans. This is not a prompt override.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_thinking_tokens",
            0,
            "A hard ceiling on the working-out for one turn. Zero means the effort level decides.",
            "thinking says how hard to work; this puts a number on it. Zero leaves the effort "
            "level to choose.",
            minimum=0,
            maximum=200_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _enum(
            "response_style",
            "natural",
            ("brief", "natural", "thorough"),
            "How long an ordinary answer should run.",
            "Does not replace persona notes. Notes teach taste; this caps length.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "stream_thinking",
            False,
            "Whether working-out is streamed to the client.",
            "Off by default: thinking is often the place secrets and half-plans show up.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "log_message_content",
            False,
            "Whether message bodies may be written to the process log.",
            "Off. Counts and digests are always logged; bodies only if this is on.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _enum(
            "permission_mode",
            "ask",
            ("ask", "accept_edits", "plan", "auto"),
            "How freely tools that change things may run.",
            "Ask is the floor for anything destructive even when this is auto.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _enum(
            "approval_policy",
            "destructive_always_asks",
            ("destructive_always_asks", "spend_and_destructive_ask"),
            "The things Lucy must ask about no matter what else you have allowed.",
            "A floor under permission_mode. An outage refuses the turn rather than guessing.",
            unavailable=OnUnavailable.REFUSE,
        ),
        _bool(
            "confirm_outward_actions",
            True,
            "Whether anything other people will see is confirmed before it happens.",
            "On asks first, whatever permission_mode says. auto cannot switch this off.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _enum(
            "input_policy",
            "enqueue",
            ("enqueue", "reject", "interrupt", "rollback"),
            "What happens if a second message arrives while a turn is running.",
            "Enqueue keeps the message. Reject tells the client to wait.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "auto_title",
            True,
            "Whether a new conversation is named from the first message.",
            "Off leaves the title as New conversation until you rename it.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "session_idle_archive_days",
            30,
            "How many idle days before an unused conversation is archived. Zero never archives.",
            "Archive hides it from the ordinary list. It is not deleted.",
            minimum=0,
            maximum=3_650,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "notify_on_long_turn",
            True,
            "Whether a turn that is taking too long says so.",
            "The turn keeps running. This is a notice, not a stop.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "long_turn_seconds",
            60,
            "How long a turn may run before that notice fires.",
            "Only used when notify_on_long_turn is on.",
            minimum=5,
            maximum=3_600,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "incognito",
            False,
            "Whether new sessions start without writing memory.",
            "On, standing personal feeds are also hidden unless you turn that off separately.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_context_tokens",
            200_000,
            "The model's effective window, as Lucy budgets it.",
            "Band shares are fractions of this number.",
            minimum=8_000,
            maximum=1_000_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "reserve_percent",
            13,
            "What share of the window is kept empty for the reply.",
            "Raising it leaves more room for a long answer and takes it from history and tools.",
            minimum=5,
            maximum=40,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "warn_at_percent",
            60,
            "How full the window gets before Lucy says so.",
            "A notice, not an action. Set it above compaction and the warning arrives too late.",
            minimum=10,
            maximum=95,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "compaction_trigger_percent",
            72,
            "How full the window may get before compaction runs.",
            "Quality is already dropping by 70%. Waiting until 95% leaves no room to summarise.",
            minimum=50,
            maximum=90,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "history_turns_kept",
            4,
            "How many recent exchanges compaction may not summarise away.",
            "Zero lets a summary replace everything up to the current turn.",
            minimum=0,
            maximum=100,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "tool_results_kept",
            3,
            "How many recent tool results survive reclamation in full.",
            "Zero means every result competes on cost. Notes results are never dropped.",
            minimum=0,
            maximum=50,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "session_token_budget",
            0,
            "A hard cap on tokens one session may spend. Zero means no cap.",
            "The turn still stops at the model's own window.",
            minimum=0,
            maximum=100_000_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_llm_turns",
            12,
            "How many model rounds one main turn may take before it stops.",
            "Each tool plan consumes another model round. The limit stops a looping model; "
            "the turn remains resumable when it is reached.",
            minimum=1,
            maximum=100,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_subagent_turns",
            8,
            "How many model rounds one child agent may take before it stops.",
            "Children get a shorter leash than the lead by default. This applies to each "
            "child independently, while concurrency and depth have their own limits.",
            minimum=1,
            maximum=50,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_tool_calls_per_turn",
            60,
            "How many tool calls one turn may make before it has to stop and report.",
            "Counted across the whole turn, not per plan.",
            minimum=1,
            maximum=500,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_turn_seconds",
            0,
            "A wall-clock ceiling on one whole turn. Zero means no ceiling.",
            "Catches a turn waiting on something slow that spends almost no tokens.",
            minimum=0,
            maximum=86_400,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "retry_attempts",
            2,
            "How many extra tries a failed downstream call gets.",
            "Zero means the first failure is the answer. 401 is never retried.",
            minimum=0,
            maximum=10,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "retry_max_seconds",
            30,
            "How long those extra tries may take in total.",
            "The clock starts at the first try. A slow 500 that already used this budget "
            "is not retried.",
            minimum=1,
            maximum=600,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "downstream_timeout_seconds",
            10,
            "How long Lucy waits for one sibling call.",
            "This is per request, not the whole turn.",
            minimum=1,
            maximum=300,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_result_token_cap",
            2_000,
            "How much a helper may hand back when it is finished.",
            "A helper returns a summary plus references, never a transcript.",
            minimum=200,
            maximum=20_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_wall_clock_seconds",
            600,
            "How long a helper may run before Lucy stops it.",
            "The helper is told it ran out of time. Its work is not discarded.",
            minimum=10,
            maximum=7_200,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_message_max_chars",
            4_000,
            "How long one helper message may be.",
            "A message over this is refused rather than truncated in place.",
            minimum=100,
            maximum=32_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_message_burst",
            5,
            "How many helper messages may arrive in one burst.",
            "A burst over this waits rather than flooding the inbox.",
            minimum=1,
            maximum=50,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _enum(
            "memory_write_policy",
            "ask_first",
            ("never", "ask_first", "automatic"),
            "Whether Lucy may write memories on its own.",
            "Never writes only on an explicit remember. Ask is the conservative outage default.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "memory_retrieval_limit",
            12,
            "How many memory topics the live index may show.",
            "The index is titles, not contents. Smaller is cheaper and usually enough.",
            minimum=0,
            maximum=100,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_max_depth",
            3,
            "How many times a child may spawn its own children.",
            "One means Lucy does everything itself. Three is the designed maximum.",
            minimum=1,
            maximum=5,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_max_concurrent",
            5,
            "How many child agents may run at once.",
            "Each child is a full context window.",
            minimum=1,
            maximum=20,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "workspace_retention_hours",
            24,
            "How long an idle workspace is kept.",
            "Shorter forgets files sooner. The sandbox expiry in live state still wins.",
            minimum=1,
            maximum=720,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "prompt_feeds_enabled",
            True,
            "Whether sibling feeds appear in the prompt at all.",
            "Off removes persona notes, now-playing, workspace shell, everything.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "prompt_hide_personal_feeds",
            True,
            "Whether incognito hides feeds marked personal.",
            "On is the conservative default. Off still does not log them.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "prompt_allow_unknown_feed_fields",
            False,
            "Whether a sibling may introduce feed keys Lucy does not know.",
            "Off. A new key is the shape of a prompt injection from a compromised service.",
            unavailable=OnUnavailable.USE_DEFAULT,
            agent=AgentAccess.NEVER,
        ),
        Knob(
            key="enabled_capabilities",
            summary="Capabilities this profile may use, when the list is not empty.",
            value_type=ValueType.STR_LIST,
            default=(),
            on_unavailable=OnUnavailable.USE_DEFAULT,
            description=(
                "Empty means stay quiet about disconnected capabilities. Naming one "
                "advertises setup for that capability in the prompt. The HTTP catalogue "
                "still lists every deployed pack so a UI can offer a connect button."
            ),
        ),
        _int(
            "max_output_tokens_per_turn",
            8_000,
            "How much Lucy may generate in a single reply.",
            "A reply that reaches the ceiling stops and says so.",
            minimum=256,
            maximum=128_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_tool_result_tokens",
            25_000,
            "How much of one tool result may become tokens before it spills.",
            "The rest stays stored and addressable by reference.",
            minimum=1_000,
            maximum=100_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_steps_per_plan",
            20,
            "How many steps one plan may hold.",
            "A refused plan is cheaper than a plan that runs for an hour.",
            minimum=1,
            maximum=100,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "max_parallel_steps",
            4,
            "How many of those steps may run at once.",
            "Never infinite: a non-finite limit collapses the pool to one worker.",
            minimum=1,
            maximum=16,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "step_timeout_seconds",
            10,
            "How long one step may take before it is marked timed out.",
            "Slow capabilities (research, external tools) get a multiple of this.",
            minimum=1,
            maximum=600,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "plan_timeout_seconds",
            60,
            "How long a whole plan may take.",
            "The turn can still continue with what finished.",
            minimum=1,
            maximum=3_600,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "render_read_tokens",
            2_000,
            "How much of a stored result the formatter may read.",
            "Raising this makes details cheaper to fetch again.",
            minimum=100,
            maximum=20_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "render_preview_tokens",
            400,
            "How much of each result is shown as a preview.",
            "Most of the time the model wants which of forty things it has, not each body.",
            minimum=50,
            maximum=5_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "render_total_tokens",
            8_000,
            "How much of one plan's rendered results may become tokens.",
            "Everything is still stored. This bounds what is shown.",
            minimum=500,
            maximum=100_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "temperature",
            100,
            "How varied the wording is, in hundredths: 100 means 1.0.",
            "This is wording, not reasoning. thinking is the effort knob.",
            minimum=0,
            maximum=200,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _bool(
            "vision_enabled",
            True,
            "Whether images in a message may be sent to the model.",
            "Off, Lucy is told an image was attached and that it may not look at it.",
            unavailable=OnUnavailable.REFUSE,
        ),
        Knob(
            key="disabled_capabilities",
            summary="Capabilities this profile must not use.",
            value_type=ValueType.STR_LIST,
            default=(),
            on_unavailable=OnUnavailable.REFUSE,
            description=(
                "A name listed here is absent from tools. Falling back to empty during an "
                "outage would re-enable something the person turned off, so the turn refuses."
            ),
        ),
    )


def _feed_knobs() -> tuple[Knob, ...]:
    caps = tuple(
        _bool(
            feed_setting_key(capability),
            True,
            f"Whether the {capability} feed appears in the prompt.",
            f"Off hides every {capability} line, including ones you left on individually.",
            unavailable=OnUnavailable.USE_DEFAULT,
        )
        for capability in capabilities()
    )
    fields = tuple(
        _bool(
            field.setting_key,
            field.default,
            field.summary,
            (
                f"One line of the {field.capability} feed ({field.volatility.value}). "
                "Off leaves the rest of that feed in place."
            ),
            unavailable=OnUnavailable.USE_DEFAULT,
        )
        for field in FIELDS
    )
    return caps + fields


def _decision_knobs() -> tuple[Knob, ...]:
    flags = (
        (
            "decisions",
            False,
            "Enable Laya-assisted decisions.",
            "Off makes no decision calls. On uses the configured decision service.",
        ),
        (
            "decision_shadow_mode",
            True,
            "Measure decisions without applying them.",
            "On records suggestions while preserving ordinary behavior.",
        ),
        (
            "decision_capabilities",
            True,
            "Preload relevant available capabilities.",
            "Only when decisions are enabled. Never executes tools or grants permission.",
        ),
        (
            "decision_memory",
            True,
            "Rank trusted memory topics by relevance.",
            "Only when decisions are enabled. Incognito sends no memories.",
        ),
        (
            "decision_recovery",
            False,
            "Suggest a new approach after repeated failures.",
            "Advisory only. Existing loop limits and approvals remain in force.",
        ),
    )
    return (
        *tuple(
            _bool(key, default, summary, description, unavailable=OnUnavailable.USE_DEFAULT)
            for key, default, summary, description in flags
        ),
        _int(
            "decision_timeout_ms",
            1000,
            "Maximum wait for one decision in milliseconds.",
            "Timeout uses ordinary behavior.",
            minimum=50,
            maximum=5000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "decision_max_per_turn",
            8,
            "Maximum decision calls in one turn.",
            "Helpers use ordinary behavior; a new main turn gets a fresh budget.",
            minimum=1,
            maximum=32,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
    )


KNOBS: tuple[Knob, ...] = (*_core(), *_feed_knobs(), *_decision_knobs())


def knob(key: str) -> Knob | None:
    return next((item for item in KNOBS if item.key == key), None)


def defaults() -> dict[str, Any]:
    return {item.key: item.default for item in KNOBS}


__all__ = [
    "KNOBS",
    "AgentAccess",
    "Knob",
    "OnUnavailable",
    "ValueType",
    "defaults",
    "knob",
]
