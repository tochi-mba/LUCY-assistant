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
