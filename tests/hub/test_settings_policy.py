"""TurnPolicy is the only place a turn reads lucy knobs, so the clamps live here."""

from __future__ import annotations

from types import SimpleNamespace

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
    assert {"help", "work", "agents"} == ALWAYS_ON


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
    assert {"disabled_capabilities", "approval_policy"} == REFUSE_KEYS
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


async def test_help_work_and_helpers_stay_bound_when_a_person_lists_them_as_disabled() -> None:
    context = Capabilities((HelpPack(), _Toy())).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    context.policy = TurnPolicy(disabled=("help", "work", "agents", "toy"))
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
    assert apply_disabled(empty, ("help", "work", "agents")) is empty


def test_a_fallback_model_must_look_like_a_provider_spec() -> None:
    policy = TurnPolicy.from_resolved(
        _Resolved({"fallback_model": "not a model", "max_thinking_tokens": True})
    )
    assert policy.fallback_model == ""
    assert policy.max_thinking_tokens == 0
