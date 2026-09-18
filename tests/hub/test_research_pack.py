"""Research is provider-gated and never returns fetched page bodies to the model."""

from __future__ import annotations

from lucy_api.clients.search import (
    Article,
    FakeSearchClient,
    Findings,
    Hit,
    Page,
    Provider,
    Summary,
)
from lucy_api.packs.base import State
from lucy_api.packs.research import ResearchPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope


def _context() -> object:
    return Capabilities(()).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="sess_a")
    )


async def test_research_is_gated_by_provider_state() -> None:
    fake = FakeSearchClient()
    pack = ResearchPack("https://search.test", client=fake)
    fake.offer([Provider("openai", "not_configured")])

    unavailable = await pack.probe(_context())

    assert unavailable.state is State.not_connected
    assert pack.setup() is not None
    fake.offer([Provider("openai", "available", model_count=2)])
    assert (await pack.probe(_context())).state is State.ready


async def test_research_operations_project_hits_pages_and_summaries() -> None:
    fake = FakeSearchClient()
    fake.seed(
        Findings(
            query="lucy",
            hits=(Hit("Lucy", "https://example.test/lucy", 1),),
            summary=Summary("Found Lucy", ("one",)),
        )
    )
    secret_page_text = "unbounded page text must not cross the tool boundary"
    fake.stock(
        Article(
            Page(
                url="https://example.test/lucy",
                final_url="https://example.test/lucy",
                title="Lucy",
                word_count=9000,
            ),
            secret_page_text,
        )
    )
    fake.summary = Summary("Short", ("point",), truncated=True, notice="bounded")
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    catalogue = await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {"id": "search", "op": "research.search", "input": {"query": "lucy"}},
                {
                    "id": "open",
                    "op": "research.open",
                    "input": {"url": "https://example.test/lucy"},
                },
                {
                    "id": "summary",
                    "op": "research.summarize",
                    "input": {"body": "long material", "topic": "Lucy"},
                },
            ]
        },
        context,
    )

    assert catalogue.ready()[0].pack.id == "research"
    assert not result["issues"]
    rendered = str(result)
    assert "https://example.test/lucy" in rendered
    assert "word_count" in rendered
    assert "Short" in rendered
    assert secret_page_text not in rendered
    assert fake.profiles == ["personal", "personal", "personal"]


async def test_empty_provider_catalogue_means_operator_not_configured() -> None:
    fake = FakeSearchClient()
    fake.offer([])
    availability = await ResearchPack("https://search.test", client=fake).probe(_context())
    assert availability.state is State.not_configured
