"""The settings pack client quotes keys and never invents a field the catalogue did not send."""

from __future__ import annotations

from lucy_api.clients.settings import FakeSettingsPackClient, HttpSettingsPackClient, Setting, _part
from lucy_api.clients.testing import Answer, FakeHttp


def test_a_key_with_a_slash_is_one_path_segment() -> None:
    assert _part("a/b") == "a%2Fb"


async def test_describe_get_and_set_use_the_settings_audience() -> None:
    row = {
        "namespace": "lucy",
        "key": "model",
        "value": "claude",
        "type": "string",
        "summary": "Which model",
        "description": "The one that answers.",
        "source": "account",
        "set": True,
        "pinned": False,
        "bounds": None,
        "scope": "profile",
    }
    http = FakeHttp(
        Answer(body={"settings": [row]}),
        Answer(body=row),
        Answer(status_code=204),
        Answer(body={**row, "value": "gpt"}),
    )
    client = HttpSettingsPackClient(http, "http://settings.test")

    described = await client.describe(profile="personal")
    got = await client.get("lucy", "model", profile="personal")
    updated = await client.set("lucy", "model", "gpt", profile="personal")

    assert described[0].key == "model"
    assert described[0].scope == "profile"
    assert got.chosen is True
    assert got.scope == "profile"
    assert updated.value == "gpt"
    assert all(call.audience == "settings" for call in http.calls)
    assert http.calls[2].json == {"value": "gpt"}
    assert all(call.params == {"profile": "personal"} for call in http.calls)


async def test_the_in_memory_catalogue_records_writes() -> None:
    fake = FakeSettingsPackClient(
        (
            Setting(namespace="lucy", key="model", value="claude", kind="string"),
            Setting(namespace="lucy", key="style", value="plain", kind="string"),
        )
    )

    listed = await fake.describe()
    updated = await fake.set("lucy", "model", "gpt")

    assert {item.key for item in listed} == {"model", "style"}
    assert updated.value == "gpt"
    assert fake.writes == [("lucy", "model", "gpt")]
    assert (await fake.get("lucy", "model")).source == "account"
