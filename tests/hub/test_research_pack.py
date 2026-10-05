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
from lucy_api.packs.collections import HIT
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
    assert fake.profiles == ["personal", "personal", "personal", "personal"]


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


async def test_the_probe_asks_about_the_profile_the_turn_runs_under() -> None:
    """The bug, named: research was declared ready on another profile's credential.

    Every operation already sent the turn's profile; the probe alone asked about the default.
    """
    fake = FakeSearchClient()
    context = Capabilities(()).context_for(
        SessionScope(account_id="acct_a", profile="work", session_id="sess_a")
    )

    await ResearchPack("https://search.test", client=fake).probe(context)

    assert fake.profiles == ["work"]


async def test_every_field_a_hit_declares_is_one_a_search_fills() -> None:
    """The bug, named: hits declared `site` and `snippet` and no search ever set either.

    `hit.filter` on a snippet matched nothing and `hit.pick` handed back empty strings, which
    reads as "the engine said nothing about this page". The service sends each result's
    snippet; it has no per-result site, so the host name in `source` is the only one there is.
    """
    fake = FakeSearchClient()
    fake.seed(
        Findings(
            query="tea",
            hits=(Hit("Tea", "https://tea.example/leaf", 1, "Leaves steeped in hot water."),),
        )
    )
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "search", "op": "research.search", "input": {"query": "tea"}}]},
        context,
    )

    (hit,) = result["steps"][0]["items"]
    assert HIT.fields is not None
    declared = [spec.name for spec in HIT.fields(None)]
    assert declared == ["title", "source", "snippet"]
    assert all(hit[name] for name in declared)
    assert hit["snippet"] == "Leaves steeped in hot water."
    assert HIT.label(hit) == "Tea (tea.example)"


async def test_one_summary_covers_a_query_and_is_paid_for_once() -> None:
    """The bug, named: the service writes one summary per query, and research.search copied
    it onto every hit, with the query and the address twice over. At five results the model
    paid for the same summary five times, and again on each later round the result stayed."""
    fake = FakeSearchClient()
    fake.seed(
        Findings(
            query="tea",
            hits=tuple(
                Hit(title=f"Tea {n}", url=f"https://tea.test/{n}", rank=n) for n in range(1, 4)
            ),
            summary=Summary("Tea is a drink.", ("Brewed hot",)),
        )
    )
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "search", "op": "research.search", "input": {"query": "tea"}}]},
        context,
    )

    hits = result["steps"][0]["data"]
    assert [hit["url"] for hit in hits] == [f"https://tea.test/{n}" for n in range(1, 4)]
    assert hits[0]["summary_of_all_results"] == {
        "executive_summary": "Tea is a drink.",
        "key_points": ["Brewed hot"],
    }
    assert all("summary_of_all_results" not in hit for hit in hits[1:])
    assert all(not {"link", "query", "summary"} & set(hit) for hit in hits)


async def test_an_empty_summary_is_no_summary() -> None:
    fake = FakeSearchClient()
    fake.seed(Findings(query="tea", hits=(Hit(title="Tea", url="https://tea.test/1"),)))
    fake.catalogue["tea"] = Findings(
        query="tea", hits=(Hit(title="Tea", url="https://tea.test/1"),), summary=Summary("")
    )
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)

    result = await capabilities.execute(
        {"steps": [{"id": "search", "op": "research.search", "input": {"query": "tea"}}]},
        context,
    )

    assert "summary_of_all_results" not in result["steps"][0]["data"][0]


async def _run(fake: FakeSearchClient, *steps: dict[str, object]) -> dict[str, object]:
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)
    return await capabilities.execute({"steps": list(steps)}, context)


def _seeded() -> FakeSearchClient:
    fake = FakeSearchClient()
    fake.seed(
        Findings(
            query="python",
            hits=tuple(
                Hit(title=f"Py {n}", url=f"https://py.test/{n}", rank=n) for n in range(1, 6)
            ),
        )
    )
    return fake


async def test_a_search_result_opens_by_reference_and_says_what_it_is_looking_for() -> None:
    """The bug, named: `research.open` took only a written-out address, though its field
    invited 'a link research.search found' -- so a small model wrote `$search[1]`, and the
    open failed with 'Only http, https URLs may be fetched'. Nor could it say what it was
    after, and the summary is all it keeps of a page."""
    fake = _seeded()

    result = await _run(
        fake,
        {"id": "found", "op": "research.search", "input": {"query": "python"}},
        {
            "id": "read",
            "op": "research.open",
            "input": {"hit": "$found[2]", "looking_for": "  the exact\nrelease date "},
        },
    )

    assert not result["issues"]
    assert fake.opened == ["https://py.test/2"]
    assert fake.looked_for == ["the exact release date"]


async def test_a_whole_search_opens_its_first_three_and_says_so() -> None:
    fake = _seeded()

    result = await _run(
        fake,
        {"id": "found", "op": "research.search", "input": {"query": "python"}},
        {"id": "read", "op": "research.open", "input": {"hit": "$found"}},
    )

    assert fake.opened == ["https://py.test/1", "https://py.test/2", "https://py.test/3"]
    assert "opened the first 3 of the 5 results referenced" in str(result["steps"][1])


async def test_a_reference_written_as_an_address_is_refused_with_the_fix() -> None:
    fake = _seeded()

    result = await _run(fake, {"id": "read", "op": "research.open", "input": {"url": "$found[1]"}})

    step = result["steps"][0]
    assert step["status"] == "error"
    assert '{"hit": "$found[1]"}' in step["error"]
    assert fake.opened == []


async def test_an_open_names_exactly_one_kind_of_source() -> None:
    fake = _seeded()
    both = await _run(
        fake,
        {"id": "found", "op": "research.search", "input": {"query": "python"}},
        {
            "id": "read",
            "op": "research.open",
            "input": {"hit": "$found[1]", "url": "https://py.test/9"},
        },
    )
    neither = await _run(fake, {"id": "read", "op": "research.open", "input": {}})
    empty = FakeSearchClient()
    empty.seed(Findings(query="none"))
    nothing = await _run(
        empty,
        {"id": "found", "op": "research.search", "input": {"query": "none"}},
        {"id": "read", "op": "research.open", "input": {"hit": "$found"}},
    )

    assert "Give `hit` or `url`, not both." in str(both["steps"][1])
    assert "Name what to open" in str(neither["steps"][0])
    assert "found no result to open" in str(nothing["steps"][1])


async def test_results_found_without_a_summary_stand_and_say_what_is_missing() -> None:
    """The bug, named: a summariser timeout failed the whole search or open in the service, so
    the model was told research failed and reported finding nothing. The results now stand,
    and a fixed sentence says only the summary is missing."""
    from lucy_api.packs.research import NO_PAGE_SUMMARY, NO_SEARCH_SUMMARY

    fake = FakeSearchClient()
    fake.seed(
        Findings(
            query="tea",
            hits=(Hit(title="Tea", url="https://tea.example/a", rank=1),),
            summary_failed=True,
        )
    )
    fake.stock(Article(Page(url="https://tea.example/a", title="Tea", word_count=40)))
    fake.summary_fails = True
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)

    searched = await capabilities.execute(
        {"steps": [{"id": "s", "op": "research.search", "input": {"query": "tea"}}]}, context
    )
    opened = await capabilities.execute(
        {"steps": [{"id": "o", "op": "research.open", "input": {"url": "https://tea.example/a"}}]},
        context,
    )

    [search_step] = searched["steps"]
    assert search_step["status"] == "ok"
    assert search_step["data"][0]["url"] == "https://tea.example/a"
    assert NO_SEARCH_SUMMARY in search_step["notices"]
    [open_step] = opened["steps"]
    assert open_step["data"]["summary"] is None
    assert NO_PAGE_SUMMARY in open_step["notices"]


async def test_a_search_that_found_nothing_does_not_mention_a_missing_summary() -> None:
    fake = FakeSearchClient()
    fake.seed(Findings(query="nothing", summary_failed=True))
    capabilities = Capabilities([ResearchPack("https://search.test", client=fake)])
    context = _context()
    await capabilities.probe(context)

    searched = await capabilities.execute(
        {"steps": [{"id": "s", "op": "research.search", "input": {"query": "nothing"}}]}, context
    )
    assert not searched["steps"][0]["notices"]
