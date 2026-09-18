"""Research projections keep page text off the search result and confess truncation."""

from __future__ import annotations

from lucy_api.clients.search import (
    Article,
    FakeSearchClient,
    Findings,
    Hit,
    HttpSearchClient,
    Page,
    Provider,
    _summary,
)
from lucy_api.clients.testing import Answer, FakeHttp


def test_a_missing_summary_is_none_and_a_truncated_one_says_so() -> None:
    assert _summary(None) is None
    assert _summary({}) is not None
    truncated = _summary(
        {
            "executive_summary": "short",
            "key_points": ["a"],
            "truncated": True,
            "chars_submitted": 10,
            "original_chars": 40,
        }
    )
    assert truncated is not None
    assert truncated.notice == "summarised 10 of 40 characters"


async def test_search_scrape_and_summarize_use_the_search_audience() -> None:
    http = FakeHttp(
        Answer(body={"providers": [{"name": "serper", "status": "available", "model_count": 1}]}),
        Answer(
            body={
                "results": [
                    {
                        "query": "tea",
                        "results": [{"title": "Tea", "url": "https://tea.example", "rank": 1}],
                        "summary": {"executive_summary": "tea is a drink"},
                    }
                ]
            }
        ),
        Answer(
            body={
                "results": [
                    {
                        "url": "https://tea.example",
                        "page": {
                            "final_url": "https://tea.example/",
                            "title": "Tea",
                            "word_count": 12,
                            "text": "full page",
                        },
                    }
                ],
                "summary": {"executive_summary": "about tea"},
            }
        ),
        Answer(body={"summary": {"executive_summary": "short"}}),
        Answer(body={}),
    )
    client = HttpSearchClient(http, "http://search.test")

    providers = await client.providers()
    found = await client.search(["tea"], profile="personal", max_results=3)
    reading = await client.scrape(["https://tea.example"], profile="personal")
    summarised = await client.summarize("long text", topic="tea", profile="personal")
    empty = await client.summarize("x")

    assert providers[0].usable is True
    assert found[0].hits[0].title == "Tea"
    assert reading.articles[0].text == "full page"
    assert summarised.executive_summary == "short"
    assert empty.executive_summary == ""
    assert all(call.audience == "web-search-api" for call in http.calls)
    assert http.calls[1].json["summarize"] is True
    assert "fetch_pages" not in http.calls[1].json


async def test_the_in_memory_web_records_what_was_asked() -> None:
    fake = FakeSearchClient()
    fake.offer((Provider(name="fake", status="available"),))
    fake.seed(Findings(query="tea", hits=(Hit("Tea", "https://tea.example", 1),)))
    fake.stock(Article(page=Page(url="https://tea.example"), text="leaf"))

    assert (await fake.providers())[0].name == "fake"
    found = await fake.search(["tea", "coffee"], profile="work", max_results=1)
    reading = await fake.scrape(["https://tea.example", "https://missing.example"], profile="work")
    summary = await fake.summarize("body", topic="tea", profile="work")

    assert found[0].hits[0].title == "Tea"
    assert found[1].hits == ()
    assert [article.page.url for article in reading.articles] == ["https://tea.example"]
    assert summary.executive_summary == "a summary"
    assert fake.asked == ["tea", "coffee", "tea"]
    assert fake.profiles == ["work", "work", "work"]
