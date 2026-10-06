"""Settings are readable, typed and changed only through an explicit write operation."""

from __future__ import annotations

from typing import Any

from lucy_api.clients.settings import FakeSettingsPackClient, Setting
from lucy_api.packs.base import State
from lucy_api.packs.service import Capabilities
from lucy_api.packs.settings import SettingsPack
from lucy_api.permissions.gate import Grant, once_key
from lucy_api.sessions.scope import SessionScope


def yes_to(context: Any, namespace: str, key: str, value: object) -> None:
    """The person's yes to one exact settings change, as an approved card leaves it."""
    arguments = {"namespace": namespace, "key": key, "value": value}
    context.grants[once_key("settings.set", arguments)] = Grant(
        "settings.write", "allow", "personal", source="person"
    )


def set_step(namespace: str, key: str, value: object) -> dict[str, Any]:
    return {
        "steps": [
            {
                "id": "set",
                "op": "settings.set",
                "input": {"namespace": namespace, "key": key, "value": value},
            }
        ]
    }


def setup() -> tuple[FakeSettingsPackClient, Capabilities, object]:
    fake = FakeSettingsPackClient(
        [
            Setting(
                "lucy",
                "max_llm_turns",
                12,
                kind="integer",
                summary="Maximum model rounds.",
                description="Stops runaway turns.",
                source="default",
                bounds={"minimum": 1, "maximum": 100},
                scope="account",
            )
        ]
    )
    capabilities = Capabilities([SettingsPack("https://settings.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a",
            profile="personal",
            session_id="sess_a",
            permission_mode="auto",
        )
    )
    for value in (20, 9, 8):
        yes_to(context, "lucy", "max_llm_turns", value)
    return fake, capabilities, context


async def test_a_with_approval_setting_asks_even_in_auto() -> None:
    """The bug, named: `max_llm_turns` is declared `with_approval` -- an assistant may propose
    it and the person confirms that change -- and in `auto` it changed without a word."""
    fake, capabilities, context = setup()
    await capabilities.probe(context)

    result = await capabilities.execute(set_step("lucy", "max_llm_turns", 50), context)

    assert result["issues"][0]["code"] == "permission_required"
    assert "needs your yes to it, whatever the mode" in result["issues"][0]["message"]
    assert fake.writes == []


async def test_a_yes_for_the_whole_conversation_does_not_cover_a_with_approval_change() -> None:
    """The confirmation is of the change, not of the assistant: "you can manage my settings"
    is not a yes to raising its own limit."""
    fake, capabilities, context = setup()
    context.grants["settings.write"] = Grant("settings.write", "allow", "personal")
    await capabilities.probe(context)

    result = await capabilities.execute(set_step("lucy", "max_llm_turns", 50), context)

    assert result["issues"][0]["code"] == "permission_required"
    assert fake.writes == []


async def test_a_sibling_setting_is_asked_about_until_its_declaration_is_known() -> None:
    fake = FakeSettingsPackClient(
        [
            Setting("search", "max_results", 5, agent="freely"),
            Setting("user", "erasure_mode", "grace", agent="never"),
        ]
    )
    capabilities = Capabilities([SettingsPack("https://settings.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a", profile="personal", session_id="sess_a", permission_mode="auto"
        )
    )
    await capabilities.probe(context)

    unseen = await capabilities.execute(set_step("search", "max_results", 8), context)
    assert unseen["issues"][0]["code"] == "permission_required", "not seen yet: asked"

    await capabilities.execute(
        {"steps": [{"id": "all", "op": "settings.describe", "input": {}}]}, context
    )
    freely = await capabilities.execute(set_step("search", "max_results", 8), context)
    assert not freely["issues"], "declared freely: auto changes it"
    never = await capabilities.execute(set_step("user", "erasure_mode", "immediate"), context)
    assert not never["issues"], "never is not asked about: a yes would change nothing"
    assert never["steps"][0]["status"] == "error"
    assert fake.writes == [("search", "max_results", 8)]


async def test_settings_pack_is_ready_and_declares_its_write_permission() -> None:
    _fake, capabilities, context = setup()
    catalogue = await capabilities.probe(context)
    pack = catalogue.ready()[0].pack

    assert catalogue.ready()[0].availability.state is State.ready
    assert {operation.name for operation in pack.operations(context)} == {
        "settings.describe",
        "settings.get",
        "settings.set",
    }
    assert pack.permissions()[0].covers == ("settings.set",)


async def test_settings_describe_get_and_set_preserve_value_types() -> None:
    fake, capabilities, context = setup()
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "describe", "op": "settings.describe", "input": {}},
                {
                    "id": "get",
                    "op": "settings.get",
                    "input": {"namespace": "lucy", "key": "max_llm_turns"},
                },
                {
                    "id": "set",
                    "op": "settings.set",
                    "input": {"namespace": "lucy", "key": "max_llm_turns", "value": 20},
                },
            ]
        },
        context,
    )

    assert not result["issues"]
    assert fake.writes == [("lucy", "max_llm_turns", 20)]
    assert "maximum" in str(result)
    assert "20" in str(result)
    assert result["steps"][0]["data"]["settings"][0]["scope"] == "account"
    assert result["steps"][1]["data"]["scope"] == "account"
    # The address is settings-api's; the placement is the capability a person would name.
    # `lucy_api.settings.groups` held that mapping and nothing read it: the model saw
    # `spotify.default_market` with no hint that it is a Music setting.
    assert result["steps"][1]["data"]["capability"] == "lucy"
    assert all("capability" in item for item in result["steps"][0]["data"]["settings"])


async def test_a_settings_write_tells_the_turn_settings_reader_what_changed() -> None:
    """The bug, named: a person turned remembering off through Lucy, and the next turn still
    asked to remember something. The write went through settings-api's person-facing routes
    and the turn's settings came from another client's minute-long cache, which nobody told."""
    _fake, capabilities, context = setup()
    changed: list[str] = []
    context.forget_settings = changed.append
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "set",
                    "op": "settings.set",
                    "input": {"namespace": "lucy", "key": "max_llm_turns", "value": 9},
                }
            ]
        },
        context,
    )
    assert not result["issues"]
    assert changed == ["lucy"]


async def test_a_settings_write_without_a_probe_cache_still_returns_the_setting() -> None:
    fake, capabilities, context = setup()
    context.probes = None
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "set",
                    "op": "settings.set",
                    "input": {"namespace": "lucy", "key": "max_llm_turns", "value": 8},
                }
            ]
        },
        context,
    )
    assert not result["issues"]
    assert fake.writes == [("lucy", "max_llm_turns", 8)]


async def test_a_setting_whose_service_did_not_say_claims_no_access() -> None:
    """An older settings-api says nothing about assistants; the row says nothing either,
    rather than guessing -- and a write to it is refused, as a `never` would be."""
    fake = FakeSettingsPackClient([Setting("memory", "write_importance_floor", 3)])
    capabilities = Capabilities([SettingsPack("https://settings.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="sess_a")
    )
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "all", "op": "settings.describe", "input": {}}]}, context
    )

    assert "assistant" not in result["steps"][0]["data"]["settings"][0]


async def test_describe_lists_one_capability_briefly_and_get_gives_it_in_full() -> None:
    """The bug, named: describe took no input and returned every setting with its long
    description -- eleven thousand characters for the lucy namespace alone, read again on every
    later round -- and never said which settings an assistant may not change, which the
    capability page told the model to check."""
    fake = FakeSettingsPackClient(
        [
            Setting("lucy", "max_llm_turns", 12, summary="Rounds.", description="Long text."),
            Setting("lucy", "prompt_sections_disabled", [], summary="Sections left out."),
            Setting(
                "spotify",
                "default_market",
                "GB",
                summary="Market.",
                description="More.",
                agent="freely",
            ),
        ]
    )
    capabilities = Capabilities([SettingsPack("https://settings.test", client=fake)])
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="sess_a")
    )
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "music", "op": "settings.describe", "input": {"capability": "Music"}},
                {"id": "all", "op": "settings.describe", "input": {}},
                {
                    "id": "one",
                    "op": "settings.get",
                    "input": {"namespace": "lucy", "key": "max_llm_turns"},
                },
            ]
        },
        context,
    )

    music, everything, one = (step["data"] for step in result["steps"])
    assert [item["key"] for item in music["settings"]] == ["default_market"]
    assert len(everything["settings"]) == 3
    assert all("description" not in item for item in everything["settings"])
    assert one["description"] == "Long text."
    access = {item["key"]: item.get("assistant") for item in everything["settings"]}
    assert access == {
        "max_llm_turns": "with_approval",
        "prompt_sections_disabled": "never",
        "default_market": "freely",
    }
