"""Research is provider-gated and never returns fetched page bodies to the model."""

from __future__ import annotations

from lucy_api.clients.search import (
    Article,
    FakeSearchClient,
    Findings,
    Hit,
    HttpSearchClient,
    Page,
    Provider,
    Summary,
)
from lucy_api.clients.testing import FakeHttp, problem
from lucy_api.packs.base import State
from lucy_api.packs.research import ResearchPack
from lucy_api.packs.service import Capabilities
from lucy_api.prompt.docs import capability_doc
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

    assert pack.docs == capability_doc("research")

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


async def test_a_search_service_that_answers_with_an_outage_leaves_research_unavailable() -> None:
    client = HttpSearchClient(FakeHttp(problem(503, detail="warming up")), "https://search.test")
    pack = ResearchPack("https://search.test", client=client)

    availability = await pack.probe(_context())

    assert availability.state is State.unavailable
    assert availability.detail == "research could not be reached"


async def test_empty_provider_catalogue_means_operator_not_configured() -> None:
    fake = FakeSearchClient()
    fake.offer([])
    availability = await ResearchPack("https://search.test", client=fake).probe(_context())
    assert availability.state is State.not_configured


async def test_opening_a_page_that_could_not_be_fetched_names_the_reason() -> None:
    """The bug, named: a blocked page reached the model as one with no title and no words.

    Nothing said it had not been read, so "this source is empty" was a fair reading of it.
    """
    fake = FakeSearchClient()
    fake.stock(
        Article(
            Page(
                url="https://example.test/private",
                status="error",
                detail="https://example.test/private disallows automated fetching.",
            )
        )
    )
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)

    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "open",
                    "op": "research.open",
                    "input": {"url": "https://example.test/private"},
                },
            ]
        },
        context,
    )

    rendered = str(result)
    assert "'status': 'error'" in rendered
    assert "'error': 'https://example.test/private disallows automated fetching.'" in rendered
    assert "could not open https://example.test/private" in rendered
