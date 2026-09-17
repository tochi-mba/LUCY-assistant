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
            default="",
            on_unavailable=OnUnavailable.USE_DEFAULT,
            description=(
                "Empty means the hub default. A named model must be one this deployment "
                "has credentials for."
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
        _enum(
            "response_style",
            "plain",
            ("plain", "brief", "thorough"),
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
            unavailable=OnUnavailable.REFUSE,
        ),
        _enum(
            "approval_policy",
            "destructive",
            ("always", "destructive", "never"),
            "When a person has to confirm a tool call.",
            "Destructive always asks, even if this is set to never — that value is refused.",
            unavailable=OnUnavailable.REFUSE,
        ),
        _enum(
            "input_policy",
            "enqueue",
            ("enqueue", "reject"),
            "What happens if a second message arrives while a turn is running.",
            "Enqueue keeps the message. Reject tells the client to wait.",
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
            "compaction_trigger_percent",
            72,
            "How full the window may get before compaction runs.",
            "Quality is already dropping by 70%. Waiting until 95% leaves no room to summarise.",
            minimum=50,
            maximum=90,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "session_token_budget",
            0,
            "A hard cap on tokens one session may spend. Zero means no cap.",
            "The turn still stops at the model's own window.",
            minimum=0,
            maximum=10_000_000,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _enum(
            "memory_write_policy",
            "ask",
            ("off", "ask", "auto"),
            "Whether Lucy may write memories on its own.",
            "Off never writes. Ask is the conservative default during an outage.",
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "memory_retrieval_limit",
            8,
            "How many memory topics the live index may show.",
            "The index is titles, not contents. Smaller is cheaper and usually enough.",
            minimum=1,
            maximum=40,
            unavailable=OnUnavailable.USE_DEFAULT,
        ),
        _int(
            "agent_max_depth",
            1,
            "How many times a child may spawn its own children.",
            "Zero means no children. One is the usual cap.",
            minimum=0,
            maximum=2,
            unavailable=OnUnavailable.REFUSE,
        ),
        _int(
            "agent_max_concurrent",
            2,
            "How many child agents may run at once.",
            "Each child is a full context window. Two is already expensive.",
            minimum=0,
            maximum=8,
            unavailable=OnUnavailable.REFUSE,
        ),
        _int(
            "workspace_retention_hours",
            72,
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
            description="Empty means all deployed capabilities. Names are product words.",
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


KNOBS: tuple[Knob, ...] = (*_core(), *_feed_knobs())


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
