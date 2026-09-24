"""Three sections, and a checked fact behind each placement.

What is pinned here is that a provider never lands in a section by accident: `ready` is a
listing call that answered, `available` is configured-but-unproven, `unavailable` carries
the sentence and the command that would move it. And that proving is cheap -- one call,
remembered -- and never touches a provider that has no key to prove.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from lucy_api.model.catalogue import CATALOGUE, spec_for
from lucy_api.model.readiness import (
    KEY_SETTING,
    MAX_DISCOVERED,
    MODEL_ID_CHARS,
    URL_SETTING,
    Readiness,
    Report,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def answering(routes: dict[str, httpx.Response]) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A transport that answers by URL and records what it was asked."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response = routes.get(str(request.url))
        if response is None:
            raise httpx.ConnectError("no route", request=request)
        return response

    return httpx.MockTransport(handle), seen


def sections(report: Report) -> dict[str, set[str]]:
    return {
        "ready": {row.provider for row in report.ready},
        "available": {row.provider for row in report.available},
        "unavailable": {row.provider for row in report.unavailable},
    }


def by_provider(report: Report, provider: str) -> Any:
    for section in (report.ready, report.available, report.unavailable):
        for row in section:
            if row.provider == provider:
                return row
    raise AssertionError(provider)


# --------------------------------------------------------------------------------------
# Every provider lands in exactly one section
# --------------------------------------------------------------------------------------


async def test_every_catalogued_provider_appears_exactly_once() -> None:
    report = await Readiness({}).report(prove=False)
    placed = [*report.ready, *report.available, *report.unavailable]
    assert sorted(row.provider for row in placed) == sorted(spec.id for spec in CATALOGUE)


async def test_nothing_configured_means_everything_is_unavailable_with_a_way_in() -> None:
    report = await Readiness({}).report(prove=False)
    assert report.ready == ()
    assert report.available == ()
    deepseek = by_provider(report, "deepseek")
    assert deepseek.detail == "no key configured"
    assert deepseek.setup is not None
    assert deepseek.setup.command == "lucy models connect deepseek"
    assert deepseek.setup.setting == KEY_SETTING
    assert deepseek.setup.console_url == "https://platform.deepseek.com"
    assert "Create a key at https://platform.deepseek.com" in deepseek.setup.instructions


async def test_a_local_runtime_that_is_off_says_how_to_switch_it_on() -> None:
    report = await Readiness({}).report(prove=False)
    ollama = by_provider(report, "ollama")
    assert ollama.local is True
    assert ollama.detail == "not switched on"
    assert ollama.setup is not None
    assert ollama.setup.setting == URL_SETTING
    assert "Start Ollama on this machine" in ollama.setup.instructions
    assert "http://localhost:11434/v1" in ollama.setup.instructions


async def test_a_provider_this_hub_cannot_use_says_so_and_offers_no_command() -> None:
    report = await Readiness({}).report(prove=False)
    vertex = by_provider(report, "vertex")
    assert vertex.detail == "cannot be used by this hub yet"
    assert vertex.setup is not None
    assert vertex.setup.command == ""
    assert "OAuth" in vertex.setup.instructions


async def test_a_row_needing_an_endpoint_points_at_the_url_setting() -> None:
    report = await Readiness({}).report(prove=False)
    azure = by_provider(report, "azure-openai")
    assert azure.setup is not None
    assert azure.setup.setting == URL_SETTING
    assert "openai.azure.com" in azure.setup.instructions
    assert azure.setup.instructions.endswith("Then supply the key.")


async def test_a_configured_provider_is_available_until_it_is_proven() -> None:
    report = await Readiness({"deepseek": "sk-x"}).report(prove=False)
    assert sections(report)["available"] == {"deepseek"}
    assert by_provider(report, "deepseek").detail == "configured; not checked yet"


async def test_a_provider_with_no_listing_endpoint_can_only_ever_be_available() -> None:
    """Some providers give no way to check a key without spending. Honesty over a guess."""
    transport, seen = answering({})
    report = await Readiness({"minimax": "sk-x"}, transport=transport).report()
    assert sections(report)["available"] == {"minimax"}
    assert "no way to check a key" in by_provider(report, "minimax").detail
    assert seen == [], "nothing was called: there is nothing to call"


# --------------------------------------------------------------------------------------
# Proving
# --------------------------------------------------------------------------------------


async def test_a_listing_that_answers_proves_the_key_and_the_call_carries_it() -> None:
    transport, seen = answering(
        {"https://api.deepseek.com/models": httpx.Response(200, json={"data": []})}
    )
    report = await Readiness({"deepseek": "sk-live"}, transport=transport).report()
    assert sections(report)["ready"] == {"deepseek"}
    assert by_provider(report, "deepseek").detail == "answered"
    assert seen[0].headers["authorization"] == "Bearer sk-live"


async def test_each_auth_shape_is_sent_the_way_the_row_says() -> None:
    transport, seen = answering(
        {
            "https://api.anthropic.com/v1/models": httpx.Response(200, json={}),
            "https://tenant.openai.azure.com/openai/v1/models": httpx.Response(200, json={}),
        }
    )
    report = await Readiness(
        {"anthropic": "sk-ant", "azure-openai": "az"},
        base_urls={"azure-openai": "https://tenant.openai.azure.com/openai/v1"},
        transport=transport,
    ).report()
    assert sections(report)["ready"] == {"anthropic", "azure-openai"}
    headers = {str(request.url): request.headers for request in seen}
    assert headers["https://api.anthropic.com/v1/models"]["x-api-key"] == "sk-ant"
    assert headers["https://api.anthropic.com/v1/models"]["anthropic-version"] == "2023-06-01"
    assert headers["https://tenant.openai.azure.com/openai/v1/models"]["api-key"] == "az"


async def test_a_refused_key_is_unavailable_with_the_reason_and_the_fix() -> None:
    transport, _ = answering({"https://api.deepseek.com/models": httpx.Response(401)})
    report = await Readiness({"deepseek": "sk-bad"}, transport=transport).report()
    row = by_provider(report, "deepseek")
    assert row.section == "unavailable"
    assert row.detail == "the key was refused"
    assert row.setup is not None
    assert row.setup.command == "lucy models connect deepseek"


async def test_a_forbidden_listing_and_a_server_error_are_named_differently() -> None:
    transport, _ = answering(
        {
            "https://api.deepseek.com/models": httpx.Response(403),
            "https://api.groq.com/openai/v1/models": httpx.Response(503),
        }
    )
    report = await Readiness({"deepseek": "a", "groq": "b"}, transport=transport).report()
    assert by_provider(report, "deepseek").detail == "the key is not allowed to list models"
    assert by_provider(report, "groq").detail == "answered 503"


async def test_an_unreachable_provider_is_named_by_the_error_type_never_the_url() -> None:
    transport, _ = answering({})
    report = await Readiness({"deepseek": "sk-x"}, transport=transport).report()
    assert by_provider(report, "deepseek").detail == "could not be reached (ConnectError)"


async def test_a_local_runtime_is_probed_at_its_health_endpoint_without_a_key() -> None:
    transport, seen = answering(
        {"http://localhost:11434/api/tags": httpx.Response(200, json={"models": []})}
    )
    report = await Readiness({"ollama": "local"}, transport=transport).report()
    assert sections(report)["ready"] == {"ollama"}
    assert by_provider(report, "ollama").detail == "running"
    assert "authorization" not in seen[0].headers


async def test_a_local_runtime_on_another_address_is_probed_there() -> None:
    transport, seen = answering(
        {"http://gpu-box:11434/v1/models": httpx.Response(200, json={"data": []})}
    )
    report = await Readiness(
        {}, base_urls={"ollama": "http://gpu-box:11434/v1"}, transport=transport
    ).report()
    assert sections(report)["ready"] == {"ollama"}
    assert str(seen[0].url) == "http://gpu-box:11434/v1/models"


async def test_a_proof_is_remembered_and_asked_again_only_after_it_ages() -> None:
    """Forty providers on every page refresh is a hammer, not a check."""
    clock = Clock()
    transport, seen = answering({"https://api.deepseek.com/models": httpx.Response(200, json={})})
    readiness = Readiness({"deepseek": "sk-x"}, transport=transport, clock=clock)

    await readiness.report()
    await readiness.report()
    assert len(seen) == 1, "the second report reused the proof"

    clock.now += 61
    await readiness.report()
    assert len(seen) == 2, "an aged proof is checked again"


async def test_the_report_serialises_to_plain_data_a_client_can_render() -> None:
    transport, _ = answering({"https://api.deepseek.com/models": httpx.Response(200, json={})})
    body = (await Readiness({"deepseek": "sk-x"}, transport=transport).report()).as_dict()
    assert set(body) == {"ready", "available", "unavailable"}
    ready = body["ready"][0]
    assert ready["provider"] == "deepseek"
    assert ready["dialect"] == "openai-chat"
    assert ready["models"] == list(spec_for("deepseek").models)  # type: ignore[union-attr]
    assert "setup" not in ready, "a ready provider needs no instructions"
    unavailable = next(row for row in body["unavailable"] if row["provider"] == "moonshot")
    assert unavailable["setup"]["command"] == "lucy models connect moonshot"


async def test_one_provider_can_be_looked_up_whichever_section_it_landed_in() -> None:
    transport, _ = answering({"https://api.deepseek.com/models": httpx.Response(200, json={})})
    report = await Readiness({"deepseek": "sk-x", "groq": "sk-y"}, transport=transport).report()
    assert report.standing("deepseek")["section"] == "ready"
    assert report.standing("groq")["section"] == "unavailable"
    assert report.standing("ollama")["section"] == "unavailable"
    with pytest.raises(KeyError):
        report.standing("gemeni")


# --------------------------------------------------------------------------------------
# What a local runtime can run is read from its own listing
# --------------------------------------------------------------------------------------

CLYDE_LISTING = {
    "object": "list",
    "data": [
        {"id": "sonnet", "object": "model", "created": 0, "owned_by": "anthropic"},
        {"id": "opus", "object": "model", "created": 0, "owned_by": "anthropic"},
        {"id": "haiku", "object": "model", "created": 0, "owned_by": "anthropic"},
        {"id": "fable", "object": "model", "created": 0, "owned_by": "anthropic"},
    ],
}
"""`GET /v1/models` from a running clyde, recorded 2026-09-24. Re-record, do not edit."""


async def test_clydes_models_are_read_from_its_own_listing() -> None:
    """The bug, named: a working clyde was reported with `models: []`, so a person choosing a
    model for a session was shown nothing to choose from."""
    transport, _ = answering(
        {"http://host.docker.internal:8127/v1/models": httpx.Response(200, json=CLYDE_LISTING)}
    )
    report = await Readiness(
        {}, base_urls={"clyde": "http://host.docker.internal:8127/v1"}, transport=transport
    ).report()
    clyde = by_provider(report, "clyde")
    assert clyde.section == "ready"
    assert clyde.models == ("sonnet", "opus", "haiku", "fable")


async def test_an_ollama_box_names_what_was_pulled_onto_it() -> None:
    transport, _ = answering(
        {
            "http://localhost:11434/api/tags": httpx.Response(
                200, json={"models": [{"name": "llama3.3:70b"}, {"name": "qwen3:8b"}]}
            )
        }
    )
    report = await Readiness({"ollama": "local"}, transport=transport).report()
    assert by_provider(report, "ollama").models == ("llama3.3:70b", "qwen3:8b")


async def test_a_remote_providers_listing_is_never_read() -> None:
    """Somebody else's catalogue, hundreds long. The row names the ones worth offering."""
    transport, _ = answering(
        {
            "https://api.anthropic.com/v1/models": httpx.Response(
                200, json={"data": [{"id": "claude-something-obscure"}]}
            )
        }
    )
    report = await Readiness({"anthropic": "sk-ant-test"}, transport=transport).report()
    anthropic = by_provider(report, "anthropic")
    assert anthropic.section == "ready"
    assert anthropic.models == spec_for("anthropic").models


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, text="OK"),
        httpx.Response(200, json=["not", "a", "listing"]),
        httpx.Response(200, json={"status": "ok"}),
        httpx.Response(200, json={"data": "not a list"}),
    ],
)
async def test_a_health_answer_that_is_not_a_listing_leaves_the_rows_own_models(
    answer: httpx.Response,
) -> None:
    """Up, and silent about its models, is not an error."""
    transport, _ = answering({"http://localhost:8127/v1/models": answer})
    report = await Readiness({"clyde": "local"}, transport=transport).report()
    clyde = by_provider(report, "clyde")
    assert clyde.section == "ready"
    assert clyde.models == spec_for("clyde").models


async def test_a_listing_keeps_only_plausible_ids_and_only_so_many() -> None:
    """Another program's output: ids only, each once, bounded in length and number."""
    rows: list[object] = [
        {"id": "  haiku  "},
        {"id": "haiku"},
        {"id": ""},
        {"id": 42},
        {"id": "x" * (MODEL_ID_CHARS + 1)},
        "not-an-object",
        *({"id": f"model-{index}"} for index in range(MAX_DISCOVERED + 10)),
    ]
    transport, _ = answering(
        {"http://localhost:8127/v1/models": httpx.Response(200, json={"data": rows})}
    )
    report = await Readiness({"clyde": "local"}, transport=transport).report()
    models = by_provider(report, "clyde").models
    assert models[0] == "haiku"
    assert models.count("haiku") == 1
    assert len(models) == MAX_DISCOVERED
    assert all(model and len(model) <= MODEL_ID_CHARS for model in models)


def test_clyde_is_catalogued_as_a_local_runtime_that_answers_in_schemas() -> None:
    """clyde honours `--json-schema` behind the chat dialect and ignores `tools` and
    `reasoning_effort`: the model it runs has had every one of its own tools removed."""
    clyde = spec_for("clyde")
    assert clyde.local is True
    assert clyde.needs_key is False
    assert clyde.traits.json_schema is True
    assert clyde.traits.tools is False
    assert clyde.traits.reasoning_effort is False
