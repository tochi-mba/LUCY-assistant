"""Research projections keep page text off the search result and confess truncation."""

from __future__ import annotations

from lucy_api.clients.search import (
    WORK_TIMEOUT_SECONDS,
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
from lucy_api.clients.transport import PROFILE_HEADER


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


async def test_a_page_the_service_could_not_fetch_says_so_instead_of_reading_as_empty() -> None:
    """The bug, named: a robots-blocked URL came back as a page with no title and no words.

    The row is the sibling's own `ScrapeResult` (Web-search-api/app/schemas/scrape.py) with its
    `ErrorPayload` (app/schemas/common.py), as `run_scrape` builds it for a `DomainError`
    (app/services/pipelines.py) -- here `ForbiddenUrlError` from app/services/fetch/page.py.
    """
    blocked = "https://tea.example/private"
    http = FakeHttp(
        Answer(
            body={
                "results": [
                    {
                        "url": blocked,
                        "status": "error",
                        "page": None,
                        "summary": None,
                        "error": {
                            "code": "forbidden_url_error",
                            "title": "Blocked by robots.txt",
                            "detail": f"{blocked} disallows automated fetching.",
                        },
                    }
                ],
                "summary": None,
            }
        )
    )

    reading = await HttpSearchClient(http, "http://search.test").scrape([blocked])

    (page,) = reading.pages()
    assert page.fetched is False
    assert page.status == "error"
    assert page.detail == f"{blocked} disallows automated fetching."


async def test_the_provider_probe_asks_about_the_profile_the_turn_runs_under() -> None:
    """The bug, named: the probe sent no profile, so it answered for the default one.

    Web-search-api reads `X-Keyring-Profile` in `get_caller` (app/api/deps.py) and falls back
    to the person's `default_profile` only when it is absent; the credential each provider is
    probed with is resolved for that profile (app/services/llm/registry.py `credential_for`).
    The body is the sibling's `ModelsResponse` with one `ProviderOut` (app/schemas/models.py).
    """
    http = FakeHttp(
        Answer(
            body={
                "default_model": "anthropic:claude-opus-5",
                "models": [],
                "providers": [
                    {
                        "name": "anthropic",
                        "status": "not_configured",
                        "detail": "No credential for this caller in keyring.",
                        "model_count": 0,
                    }
                ],
            }
        )
    )

    providers = await HttpSearchClient(http, "http://search.test").providers(profile="work")

    assert http.last.method == "GET"
    assert http.last.url == "http://search.test/v1/models"
    assert http.last.headers == {PROFILE_HEADER: "work"}
    assert providers[0].status == "not_configured"


async def test_the_in_memory_web_records_what_was_asked() -> None:
    fake = FakeSearchClient()
    fake.offer((Provider(name="fake", status="available"),))
    fake.seed(Findings(query="tea", hits=(Hit("Tea", "https://tea.example", 1),)))
    fake.stock(Article(page=Page(url="https://tea.example"), text="leaf"))

    assert (await fake.providers(profile="work"))[0].name == "fake"
    found = await fake.search(["tea", "coffee"], profile="work", max_results=1)
    reading = await fake.scrape(["https://tea.example", "https://missing.example"], profile="work")
    summary = await fake.summarize("body", topic="tea", profile="work")

    assert found[0].hits[0].title == "Tea"
    assert found[1].hits == ()
    assert [article.page.url for article in reading.articles] == [
        "https://tea.example",
        "https://missing.example",
    ]
    assert [article.page.fetched for article in reading.articles] == [True, False]
    assert reading.articles[1].page.detail == "https://missing.example responded 404."
    assert summary.executive_summary == "a summary"
    assert fake.asked == ["tea", "coffee", "tea"]
    assert fake.profiles == ["work", "work", "work", "work"]


# --- a health check and a page fetch are not the same wait -------------------------------------
#
# `GET /v1/models` answers in milliseconds; a scrape drives a headless browser and then a model,
# and a single page measured 15 seconds warm. Against the one `http_timeout_seconds` the call
# was abandoned at ten, retried, abandoned again, and the step died at its own ceiling reporting
# a timeout that had already happened three times underneath it.


async def test_a_scrape_waits_longer_than_a_health_check() -> None:
    http = FakeHttp(Answer(body={"results": [], "summary": None}))
    await HttpSearchClient(http, "http://search.test").scrape(["https://example.invalid"])
    assert http.last.timeout_seconds == WORK_TIMEOUT_SECONDS
    assert WORK_TIMEOUT_SECONDS > 10


async def test_a_search_waits_longer_too() -> None:
    http = FakeHttp(Answer(body={"results": []}))
    await HttpSearchClient(http, "http://search.test").search(["anything"])
    assert http.last.timeout_seconds == WORK_TIMEOUT_SECONDS


async def test_a_summarise_waits_longer_too() -> None:
    http = FakeHttp(Answer(body={"summary": None}))
    await HttpSearchClient(http, "http://search.test").summarize("a long body")
    assert http.last.timeout_seconds == WORK_TIMEOUT_SECONDS


async def test_the_provider_probe_keeps_the_short_wait() -> None:
    """A provider list that has not arrived in ten seconds is one that is not coming."""
    http = FakeHttp(Answer(body=[]))
    await HttpSearchClient(http, "http://search.test").providers()
    assert http.last.timeout_seconds is None
