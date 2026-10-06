"""A person can leave parts of the standing prompt out, and never the two that keep it safe.

The new behaviour, named: `lucy.prompt_sections_disabled`. `render_all` and the context
builder could always leave a section out, and nothing fed them, so a person who never codes
paid for the sandbox guidance on every turn. The list is read once per turn, filtered to the
sections a person may drop, and applied to what is sent and to what is counted alike.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.clients.settings import FakeSettingsPackClient, Setting
from lucy_api.packs.service import Capabilities
from lucy_api.packs.settings import SettingsPack
from lucy_api.prompt.sections import BUILTIN, PromptContext, render_all
from lucy_api.sessions.scope import SessionScope
from lucy_api.settings.catalogue import PROMPT_SECTIONS, AgentAccess, OnUnavailable, knob
from lucy_api.settings.manner import PREAMBLE
from lucy_api.settings.policy import OPTIONAL_SECTIONS, TurnPolicy
from lucy_api.turn.prompt import (
    SessionView,
    context_for_session,
    projected_rows,
    system_and_messages,
    view_limits,
)

if TYPE_CHECKING:
    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings

SANDBOX = "## Working in the sandbox"


class _Resolved:
    """A resolved namespace, as settings-client hands it."""

    def __init__(self, values: dict[str, object], *, refused: frozenset[str] = frozenset()) -> None:
        self._values = values
        self.refused = refused

    def get(self, key: str, default: object = None) -> object:
        return self._values.get(key, default)


def policy(value: object, **options: Any) -> TurnPolicy:
    return TurnPolicy.from_resolved(_Resolved({"prompt_sections_disabled": value}, **options))


def view(disabled: object = ()) -> SessionView:
    return SessionView(
        session_id="ses_sections",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "what is on today",
                "turn_id": "trn_1",
                "type": "message",
            }
        ],
        session={"profile": "personal", "title": "", "permission_mode": "ask", "incognito": 0},
        capabilities=("agents", "notes", "workspace"),
        **view_limits(policy(disabled)),
    )


# --------------------------------------------------------------------------------------
# Reading the list
# --------------------------------------------------------------------------------------


def test_nothing_listed_leaves_every_section_in() -> None:
    assert TurnPolicy.from_resolved(_Resolved({})).prompt_sections_disabled == ()
    assert TurnPolicy().prompt_sections_disabled == ()


def test_the_list_is_read_in_the_order_given_without_repeats() -> None:
    assert policy(["workspace", "helpers", "workspace"]).prompt_sections_disabled == (
        "workspace",
        "helpers",
    )


@pytest.mark.parametrize(
    "listed",
    [
        ["tools"],
        ["safety"],
        ["identity"],
        ["capabilities"],
        ["person"],
        ["preferences"],
        ["memories"],
        ["Workspace"],
        ["goals"],
        [7, None, ""],
        "workspace",
        None,
    ],
)
def test_a_section_a_person_may_not_drop_is_dropped_from_the_list_instead(listed: object) -> None:
    """`render_all` refuses `tools` and `safety` by raising, which would fail every turn. A
    name that is not offered never reaches it: the turn runs with the section in."""
    assert policy(listed).prompt_sections_disabled == ()


def test_the_protected_pair_is_filtered_out_and_the_rest_kept() -> None:
    chosen = policy(["safety", "workspace", "tools", "context"])

    assert chosen.prompt_sections_disabled == ("workspace", "context")
    render_all(
        PromptContext(capabilities=("agents", "notes", "workspace")),
        disabled=chosen.prompt_sections_disabled,
    )


def test_a_list_that_cannot_be_resolved_is_not_guessed() -> None:
    refused = policy(["workspace"], refused=frozenset({"prompt_sections_disabled"}))

    assert refused.prompt_sections_disabled == ()
    assert refused.blocks_turn is False


def test_what_may_be_dropped_is_exactly_the_sections_that_allow_it() -> None:
    sections = {section.id: section for section in BUILTIN}

    assert frozenset(PROMPT_SECTIONS) == OPTIONAL_SECTIONS
    assert all(sections[name].disableable for name in PROMPT_SECTIONS)
    assert "tools" not in OPTIONAL_SECTIONS
    assert "safety" not in OPTIONAL_SECTIONS
    assert list(PROMPT_SECTIONS) == [s.id for s in BUILTIN if s.id in OPTIONAL_SECTIONS]


# --------------------------------------------------------------------------------------
# What is sent, and what is counted
# --------------------------------------------------------------------------------------


def test_the_view_takes_the_list_from_the_policy() -> None:
    assert view_limits(policy(["helpers"]))["disabled_sections"] == ("helpers",)
    assert view_limits(SimpleNamespace())["disabled_sections"] == ()


async def test_a_dropped_section_is_not_sent_and_the_rest_is_unchanged() -> None:
    whole, _messages = await system_and_messages(view())
    trimmed, _messages = await system_and_messages(view(["workspace"]))

    assert SANDBOX in whole
    assert SANDBOX not in trimmed
    workspace = next(
        s
        for s in render_all(PromptContext(capabilities=("agents", "notes", "workspace")))
        if s.id == "workspace"
    ).body
    assert whole.replace(f"{workspace}\n\n", "") == trimmed


async def test_the_context_view_shows_what_a_turn_sends() -> None:
    document = await context_for_session(view(["workspace", "goals"]))

    assert "workspace" not in {section["id"] for section in document["sections"]}
    assert SANDBOX not in document["prompt"]


async def test_a_profile_s_choices_reach_its_conversation_s_context(
    settings: Settings, keyring: FakeKeyring
) -> None:
    """End to end: profile-scoped values seeded the way settings-api returns them, read for
    the session's profile, and shown by `GET /v1/sessions/{id}/context`."""
    preferences = FakeSettingsClient()
    preferences.seed(
        "lucy",
        {"prompt_sections_disabled": ["workspace", "safety"], "opinions": "only_when_asked"},
        profile="personal",
    )
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        await app.state.container.preferences.aclose()
        app.state.container.preferences = preferences
        app.state.container.environment_override = FakeEnvironmentsClient()
        created = await http.post(
            "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "sections"}
        )
        assert created.status_code == 201, created.text
        context = await http.get(f"/v1/sessions/{created.json()['id']}/context", headers=bearer())

    assert context.status_code == 200, context.text
    prompt = context.json()["prompt"]
    assert SANDBOX not in prompt
    assert "## What you never do" in prompt
    assert f"{PREAMBLE} Give an opinion only when they ask for one" in prompt
    assert ("lucy", "personal") in preferences.asked


def test_the_window_counts_the_prompt_that_is_actually_sent() -> None:
    """A dropped section still counted would tell the person the window was fuller than it
    is, and compact their conversation early for text the model never saw."""
    workspace = next(
        s
        for s in render_all(PromptContext(capabilities=("agents", "notes", "workspace")))
        if s.id == "workspace"
    ).tokens

    _rows, whole = projected_rows(view())
    _rows, trimmed = projected_rows(view(["workspace"]))

    assert whole.used - trimmed.used == workspace


# --------------------------------------------------------------------------------------
# Only the person may change it
# --------------------------------------------------------------------------------------


def test_the_catalogue_says_no_assistant_may_write_it() -> None:
    item = knob("prompt_sections_disabled")

    assert item is not None
    assert item.default == ()
    assert item.choices == PROMPT_SECTIONS
    assert item.agent is AgentAccess.NEVER
    assert item.on_unavailable is OnUnavailable.USE_DEFAULT


def _pack(*settings: Setting) -> tuple[FakeSettingsPackClient, Capabilities, Any]:
    fake = FakeSettingsPackClient(settings)
    capabilities = Capabilities([SettingsPack("https://settings.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a",
            profile="personal",
            session_id="sess_a",
            permission_mode="auto",
        )
    )
    return fake, capabilities, context


def _set(namespace: str, key: str, value: object) -> dict[str, Any]:
    return {
        "steps": [
            {
                "id": "set",
                "op": "settings.set",
                "input": {"namespace": namespace, "key": key, "value": value},
            }
        ]
    }


@pytest.mark.parametrize("key", ["prompt_sections_disabled", "prompt_allow_unknown_feed_fields"])
async def test_a_model_cannot_write_a_setting_only_the_person_may_change(key: str) -> None:
    """The bug, named: `agent: never` was declared and nothing in the hub applied it, so a
    model in `auto` could leave sections out of its own instructions, or let a sibling
    invent feed keys, with one `settings.set`."""
    fake, capabilities, context = _pack(Setting("lucy", key, [], kind="str_list"))
    await capabilities.probe(context)

    result = await capabilities.execute(_set("lucy", key, ["helpers"]), context)

    step = result["steps"][0]
    assert fake.writes == []
    assert step["status"] == "error"
    assert f"lucy.{key} can only be changed by the person" in step["error"]
    assert "Tell them where to change it." in step["error"]


async def test_the_same_key_in_another_namespace_is_that_namespace_s_business() -> None:
    fake, capabilities, context = _pack(
        Setting("persona", "prompt_sections_disabled", [], agent="with_approval")
    )
    await capabilities.probe(context)

    result = await capabilities.execute(
        _set("persona", "prompt_sections_disabled", ["notes"]), context
    )

    assert not result["issues"]
    assert fake.writes == [("persona", "prompt_sections_disabled", ["notes"])]


@pytest.mark.parametrize("declared", ["never", ""])
async def test_a_sibling_setting_no_assistant_may_change_is_refused(declared: str) -> None:
    """The bug, named: only `lucy.*` was checked against what an assistant may change, and
    settings-api enforces nothing itself, so a model could switch off `user.erasure_mode` or a
    memory protection -- every setting in user, keyring and memory is `never` -- without a
    word in `auto`. A setting that says nothing is treated as `never`, as settings-api says."""
    fake, capabilities, context = _pack(Setting("user", "erasure_mode", "grace", agent=declared))
    await capabilities.probe(context)

    result = await capabilities.execute(_set("user", "erasure_mode", "immediate"), context)

    step = result["steps"][0]
    assert step["status"] == "error"
    assert "user.erasure_mode can only be changed by the person" in step["error"]
    assert fake.writes == []
