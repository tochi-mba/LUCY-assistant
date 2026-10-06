"""Settings are readable, typed and changed only through an explicit write operation."""

from __future__ import annotations

from lucy_api.clients.settings import FakeSettingsPackClient, Setting
from lucy_api.packs.base import State
from lucy_api.packs.service import Capabilities
from lucy_api.packs.settings import SettingsPack
from lucy_api.sessions.scope import SessionScope


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
    return fake, capabilities, context


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
