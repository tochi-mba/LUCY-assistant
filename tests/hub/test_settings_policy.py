"""TurnPolicy is the only place a turn reads lucy knobs, so the clamps live here."""

from __future__ import annotations

from types import SimpleNamespace

from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Catalogue, State
from lucy_api.packs.help import HelpPack
from lucy_api.packs.registry import apply_disabled, probe_all
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope
from lucy_api.settings.policy import ALWAYS_ON, REFUSE_KEYS, TurnPolicy, _text


class _Resolved:
    """A mapping-shaped object with an explicit refused set, like settings-client."""

    def __init__(self, values: dict[str, object], *, refused: frozenset[str] = frozenset()) -> None:
        self._values = values
        self.refused = refused

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)


class _Toy:
    """A capability a person may turn off, unlike help."""

    id = "toy"
    title = "Toy"
    summary = "A fictional capability used only to prove disablement."

    @property
    def docs(self) -> None:
        return None

    def permissions(self) -> tuple[object, ...]:
        return ()

    def result_trust(self, operation: str, data: object) -> Trust:

        del operation, data

        return Trust.untrusted

    def setup(self) -> None:
        return None

    async def probe(self, _context: object) -> Availability:
        return Availability(state=State.ready)

    def operations(self, _context: object) -> tuple[object, ...]:
        return ()


def test_an_unreachable_store_blocks_the_turn_rather_than_guessing() -> None:
    assert TurnPolicy.from_resolved(None).blocks_turn is True
    assert TurnPolicy.from_resolved(object()).blocks_turn is True


def test_defaults_match_the_catalogue_when_nothing_is_set() -> None:
    policy = TurnPolicy.from_resolved(_Resolved({}))
    assert policy.blocks_turn is False
    assert policy.model == "anthropic:claude-opus-5"
    assert policy.fallback_model == ""
    assert policy.max_thinking_tokens == 0
    assert policy.confirm_outward_actions is True
    assert policy.auto_title is True
    assert policy.notify_on_long_turn is True
    assert policy.long_turn_seconds == 60
    assert policy.retry_attempts == 2
    assert policy.retry_max_seconds == 30
    assert policy.downstream_timeout_seconds == 10
    assert policy.agent_wall_clock_seconds == 600
    assert policy.agent_message_max_chars == 4_000
    assert policy.agent_message_burst == 5
    assert policy.enabled == ()
    assert policy.thinking == "medium"
    assert policy.stream_thinking is False
    assert policy.log_message_content is False
    assert policy.incognito is False
    assert policy.temperature == 1.0
    assert policy.response_style == "natural"
    assert policy.max_output_tokens == 8_000
    assert policy.max_tool_result_tokens == 25_000
    assert policy.max_tool_calls == 60
    assert policy.max_llm_turns == 12
    assert policy.max_subagent_turns == 8
    assert policy.max_steps == 20
    assert policy.max_parallel == 4
    assert policy.step_timeout_ms == 10_000
    assert policy.plan_timeout_ms == 60_000
    assert policy.agent_max_depth == 3
    assert policy.agent_result_token_cap == 2_000
    assert policy.memory_retrieval_limit == 12
    assert policy.memory_write_policy == "ask_first"
    assert policy.permission_mode == "ask"
    assert policy.input_policy == "enqueue"
    assert policy.approval_policy == "destructive_always_asks"
    assert policy.max_context_tokens == 200_000
    assert policy.reserve_percent == 13
    assert policy.warn_at_percent == 60
    assert policy.compaction_trigger_percent == 72
    assert policy.history_turns_kept == 4
    assert policy.tool_results_kept == 3
    assert policy.session_token_budget == 0
    assert policy.disabled == ()


def test_live_values_are_clamped_and_temperature_is_hundredths() -> None:
    policy = TurnPolicy.from_resolved(
        _Resolved(
            {
                "temperature": 50,
                "max_llm_turns": 3,
                "max_output_tokens_per_turn": 512,
                "max_steps_per_plan": 2,
                "step_timeout_seconds": 7,
                "plan_timeout_seconds": 9,
                "response_style": "brief",
                "thinking": "high",
                "stream_thinking": True,
                "log_message_content": True,
                "incognito": True,
                "disabled_capabilities": ["toy", "help", "work", 1],
                "memory_write_policy": "never",
                "input_policy": "reject",
                "max_context_tokens": 32_000,
                "tool_results_kept": 1,
                "session_token_budget": 50_000,
                "fallback_model": "openai:gpt-4.1",
                "max_thinking_tokens": 2_048,
                "confirm_outward_actions": False,
                "enabled_capabilities": ["music", "help"],
                "retry_attempts": 0,
                "agent_message_burst": 2,
            }
        )
    )
    assert policy.temperature == 0.5
    assert policy.max_llm_turns == 3
    assert policy.max_output_tokens == 512
    assert policy.max_steps == 2
    assert policy.step_timeout_ms == 7_000
    assert policy.plan_timeout_ms == 9_000
    assert policy.response_style == "brief"
    assert policy.thinking == "high"
    assert policy.stream_thinking is True
    assert policy.log_message_content is True
    assert policy.incognito is True
    assert policy.memory_write_policy == "never"
    assert policy.input_policy == "reject"
    assert policy.max_context_tokens == 32_000
    assert policy.tool_results_kept == 1
    assert policy.session_token_budget == 50_000
    assert policy.fallback_model == "openai:gpt-4.1"
    assert policy.max_thinking_tokens == 2_048
    assert policy.confirm_outward_actions is False
    assert policy.enabled == ("music", "help")
    assert policy.retry_attempts == 0
    assert policy.agent_message_burst == 2
    assert policy.disabled == ("toy",)
    assert "help" not in policy.disabled
    assert {"help", "work"} == ALWAYS_ON, "helpers can be turned off; help and work cannot"


def test_wrong_types_and_unknown_enums_fall_back_to_the_designed_value() -> None:
    policy = TurnPolicy.from_resolved(
        _Resolved(
            {
                "temperature": True,
                "max_llm_turns": "12",
                "response_style": "verbose",
                "thinking": "maximum",
                "disabled_capabilities": "music",
                "vision_enabled": "yes",
                "stream_thinking": "yes",
                "log_message_content": "yes",
                "incognito": "yes",
                "input_policy": "shove",
            }
        )
    )
    assert policy.temperature == 1.0
    assert policy.max_llm_turns == 12
    assert policy.response_style == "natural"
    assert policy.thinking == "medium"
    assert policy.disabled == ()
    assert policy.vision_enabled is True
    assert policy.stream_thinking is False
    assert policy.log_message_content is False
    assert policy.incognito is False
    assert policy.input_policy == "enqueue"
    assert _text("", "natural") == "natural"
    assert _text("verbose", "natural", allowed=frozenset({"brief", "natural"})) == "natural"
    assert _text("brief", "natural", allowed=frozenset({"brief", "natural"})) == "brief"
    assert _text("kept", "natural") == "kept"


def test_out_of_range_numbers_are_clamped_not_rejected() -> None:
    policy = TurnPolicy.from_resolved(
        _Resolved(
            {
                "max_llm_turns": 0,
                "max_output_tokens_per_turn": 10,
                "temperature": 9_000,
                "memory_retrieval_limit": -4,
            }
        )
    )
    assert policy.max_llm_turns == 1
    assert policy.max_output_tokens == 256
    assert policy.temperature == 2.0
    assert policy.memory_retrieval_limit == 0


def test_a_refused_safety_key_blocks_the_turn_and_a_refused_vision_key_does_not() -> None:
    blocked = TurnPolicy.from_resolved(_Resolved({}, refused=frozenset({"disabled_capabilities"})))
    assert {"disabled_capabilities", "approval_policy", "act_unattended"} == REFUSE_KEYS
    assert blocked.blocks_turn is True
    assert blocked.disabled == ()

    vision = TurnPolicy.from_resolved(_Resolved({}, refused=frozenset({"vision_enabled"})))
    assert vision.blocks_turn is False
    assert vision.vision_enabled is True

    both = TurnPolicy.from_resolved(
        _Resolved({}, refused=frozenset({"approval_policy", "vision_enabled"}))
    )
    assert both.blocks_turn is True


def test_a_resolved_object_without_a_callable_get_is_treated_as_an_outage() -> None:
    assert TurnPolicy.from_resolved(SimpleNamespace(get="nope")).blocks_turn is True


async def test_help_and_work_stay_bound_when_a_person_lists_them_as_disabled() -> None:
    context = Capabilities((HelpPack(), _Toy())).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    context.policy = TurnPolicy(disabled=("help", "work", "toy"))
    catalogue = await probe_all((HelpPack(), _Toy()), context)
    toy = catalogue.get("toy")
    help_bound = catalogue.get("help")
    assert toy is not None
    assert toy.availability.state is State.disabled
    assert toy.operations == ()
    assert toy.availability.detail == "turned off in settings"
    assert help_bound is not None
    assert help_bound.availability.state is State.ready
    assert help_bound.operations


def test_apply_disabled_returns_the_same_catalogue_when_only_always_on_names_were_listed() -> None:
    empty = Catalogue(bound=())
    assert apply_disabled(empty, ("help", "work")) is empty


def test_a_session_narrows_the_profile_and_never_widens_it() -> None:
    profile = TurnPolicy(disabled=("music",))

    narrowed = profile.for_session(("agents", "help", "music", "agents"))

    assert narrowed.disabled == ("music",), "the profile's own list is untouched"
    assert narrowed.session_disabled == ("agents", "music")
    assert narrowed.all_disabled == ("music", "agents")
    assert narrowed.for_session(()).all_disabled == ("music",), "a later list replaces it"
    assert profile.all_disabled == ("music",)


def test_a_fallback_model_must_look_like_a_provider_spec() -> None:
    policy = TurnPolicy.from_resolved(
        _Resolved({"fallback_model": "not a model", "max_thinking_tokens": True})
    )
    assert policy.fallback_model == ""
    assert policy.max_thinking_tokens == 0


def test_the_unattended_settings_default_to_what_lucy_did_before_they_existed() -> None:
    """New behaviour: four settings, and nobody choosing them changes nothing."""
    policy = TurnPolicy.from_resolved(_Resolved({}))
    assert policy.act_unattended is True
    assert policy.quiet_hours == ""
    assert policy.quiet is None
    assert policy.wake_by_default is True
    assert policy.watch_default_minutes == 5


def test_the_unattended_settings_are_read_checked_and_clamped() -> None:
    """New behaviour: a chosen window is read on the person's clock; odd values are not used."""
    chosen = TurnPolicy.from_resolved(
        _Resolved(
            {
                "act_unattended": False,
                "quiet_hours": "23:00-07:00",
                "timezone": "Europe/London",
                "wake_by_default": False,
                "watch_default_minutes": 20,
            }
        )
    )
    assert chosen.act_unattended is False
    assert chosen.wake_by_default is False
    assert chosen.watch_default_minutes == 20
    assert chosen.quiet is not None
    assert chosen.quiet.tag() == "23:00-07:00@Europe/London"

    odd = TurnPolicy.from_resolved(
        _Resolved(
            {
                "act_unattended": "no",
                "quiet_hours": "11pm-7am",
                "wake_by_default": 0,
                "watch_default_minutes": 600,
            }
        )
    )
    assert odd.act_unattended is True
    assert odd.quiet_hours == ""
    assert odd.wake_by_default is True
    assert odd.watch_default_minutes == 60
    assert (
        TurnPolicy.from_resolved(_Resolved({"watch_default_minutes": 0})).watch_default_minutes == 1
    )


def test_a_refused_act_unattended_blocks_the_turn_and_never_reads_as_permission() -> None:
    """New behaviour: an outage cannot be guessed as "Lucy may act while you are away".

    The turn is blocked, as for every refuse key; and anything that reads the policy without
    honouring that verdict still finds the setting off rather than the permissive default.
    """
    refused = TurnPolicy.from_resolved(
        _Resolved({"act_unattended": True}, refused=frozenset({"act_unattended"}))
    )
    assert refused.blocks_turn is True
    assert refused.act_unattended is False


def test_the_four_customisation_settings_default_to_what_lucy_did_before_them() -> None:
    """Nobody chose: helpers on the conversation's model, archives kept for ever, the
    built-in binding order, and the full exact-whitespace-fuzzy edit ladder."""
    for policy in (TurnPolicy.from_resolved(_Resolved({})), TurnPolicy.from_resolved(None)):
        assert policy.helper_model == ""
        assert policy.delete_archived_sessions_after_days == 0
        assert policy.preferred_capabilities == ()
        assert policy.workspace_edit_matching == "fuzzy"


def test_the_customisation_settings_are_read_and_clamped() -> None:
    """A chosen value is used; out-of-range days clamp, a bad spelling falls back, and the
    preferred list is de-duplicated and held to sixteen."""
    names = [f"cap{index}" for index in range(20)]
    policy = TurnPolicy.from_resolved(
        _Resolved(
            {
                "helper_model": "openai:gpt-mini",
                "delete_archived_sessions_after_days": 90,
                "preferred_capabilities": ["music", "", 7, "music", "repos", *names],
                "workspace_edit_matching": "exact",
            }
        )
    )
    assert policy.helper_model == "openai:gpt-mini"
    assert policy.delete_archived_sessions_after_days == 90
    assert policy.preferred_capabilities[:3] == ("music", "repos", "cap0")
    assert len(policy.preferred_capabilities) == 16
    assert policy.workspace_edit_matching == "exact"

    wild = TurnPolicy.from_resolved(
        _Resolved(
            {
                "helper_model": "no colon",
                "delete_archived_sessions_after_days": 99_999,
                "preferred_capabilities": "music",
                "workspace_edit_matching": "loose",
            }
        )
    )
    assert wild.helper_model == ""
    assert wild.delete_archived_sessions_after_days == 3_650
    assert wild.preferred_capabilities == ()
    assert wild.workspace_edit_matching == "fuzzy"
    negative = _Resolved({"delete_archived_sessions_after_days": -5})
    assert TurnPolicy.from_resolved(negative).delete_archived_sessions_after_days == 0
