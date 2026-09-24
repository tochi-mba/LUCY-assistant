"""Research, where the whole context budget is won or lost.

In one measured baseline, tool results were 96.3% of a research agent's context, and almost
all of it was page text. A single scraped article is a few thousand tokens; ten of them is a
turn nobody can afford and a model that has stopped being able to see the question.

So the projection is the one the plan names: an executive summary, its key points, and URLs.
A search result keeps its title, its link and its rank -- enough to cite, enough to choose
what to open -- and its extracted page text does not survive at all. A scrape keeps the text,
because fetching a page and throwing the text away would be absurd, but keeps it in a field
of its own on `Article`: that is what goes to the workspace and comes back as a reference,
and `Article.page` is the part a tool result is allowed to carry.

Summaries confess. The service already reports whether the source text was truncated and how
many characters of how many it actually read, so `Summary.notice` says so in the words the
rest of the hub uses. A summary of a third of an article that does not mention it is worse
than no summary, because nothing downstream can tell.

The probe is `GET /v1/models`: it answers per caller, so a provider reported
`not_configured` while a token is present means "this person has not connected it" rather
than "the operator has not deployed it".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from lucy_api.clients.transport import Sibling, flag, given, nested, number, rows, text

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from lucy_api.packs.context import Http

SERVICE = "search"
AUDIENCE = "web-search-api"

WORK_TIMEOUT_SECONDS = 90.0
"""How long to wait on a call that fetches and summarises, rather than one that answers.

`GET /v1/models` is a health check and keeps the default: a provider list that has not
arrived in ten seconds is a provider list that is not coming. Searching and scraping are
neither -- each one drives a headless browser and then a model, and a single page measured
15 seconds warm. Against the default the call was abandoned at ten, retried, and abandoned
again, and the step died at its own ceiling reporting a timeout that had already happened
three times underneath it. The same shape as the sandbox: one figure for "is it up" and for
"do this", and it can only be right for one of them.
"""


DEFAULT_RESULTS = 5
"""Results per query. The service will give more; a context window would rather it did not."""

AVAILABLE = "available"
NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class Provider:
    """One model provider and whether this person can actually use it.

    The status is the service's own word -- `available`, `unauthorized`, `unreachable`,
    `not_configured` -- because the distinction between "the operator never deployed this"
    and "this person has not connected it" lives in whether a token was presented, which is
    the caller's knowledge and not this client's.
    """

    name: str
    status: str
    detail: str = ""
    model_count: int = 0

    @property
    def usable(self) -> bool:
        return self.status == AVAILABLE


@dataclass(frozen=True, slots=True)
class Summary:
    """What was read, said briefly, with what was left out admitted."""

    executive_summary: str = ""
    key_points: tuple[str, ...] = ()
    truncated: bool = False
    notice: str = ""


@dataclass(frozen=True, slots=True)
class Hit:
    """One search result: enough to cite it and enough to decide whether to open it."""

    title: str
    url: str
    rank: int = 0


@dataclass(frozen=True, slots=True)
class Findings:
    """The outcome of one query in a batch.

    One failing query never fails a batch, so the status is per query and a caller reads it
    rather than catching something.
    """

    query: str
    status: str = "ok"
    hits: tuple[Hit, ...] = ()
    summary: Summary | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Page:
    """Where a page came from and how much of it there was. The part a model may see.

    A URL the service could not fetch -- robots.txt, a blocked address, a 404, a timeout --
    is still a row in a 200, with no page and the reason in its `error`. Read without its
    `status` it became a page with no title and no words, which is also exactly what an
    empty page looks like, so a model could cite a source nobody had read and the service's
    "https://example.com/private disallows automated fetching." went nowhere.
    """

    url: str
    final_url: str = ""
    title: str = ""
    word_count: int = 0
    status: str = "ok"
    detail: str = ""

    @property
    def fetched(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class Article:
    """One fetched page, with its text kept apart from the part that may be rendered.

    `text` is the only field in this module that must never reach a tool result. It is
    written to the workspace and comes back as a reference; `page` is what the model reads
    in the meantime. Keeping them in one object rather than two parallel lists means nothing
    has to re-associate a body with the URL it came from.
    """

    page: Page
    text: str = ""


@dataclass(frozen=True, slots=True)
class Reading:
    """A batch of fetched pages and the summary made from them."""

    articles: tuple[Article, ...] = ()
    summary: Summary | None = None

    def pages(self) -> tuple[Page, ...]:
        """What a tool result is built from: the pages, never the text."""
        return tuple(article.page for article in self.articles)


class SearchClient(Protocol):
    """Search, open, summarise. Three operations, because a model needs no more."""

    async def providers(self, *, profile: str = "") -> tuple[Provider, ...]:
        """Which model providers this person can currently use, under this profile."""
        ...

    async def search(
        self, queries: Sequence[str], *, profile: str = "", max_results: int = DEFAULT_RESULTS
    ) -> tuple[Findings, ...]:
        """Run a batch of queries, summarised, and answer one result set per query."""
        ...

    async def scrape(self, urls: Sequence[str], *, profile: str = "") -> Reading:
        """Fetch pages and summarise them together."""
        ...

    async def summarize(self, body: str, *, topic: str = "", profile: str = "") -> Summary:
        """Summarise text this hub already holds."""
        ...


class HttpSearchClient:
    """The real client, over the one seam a capability has to a sibling."""

    def __init__(self, http: Http, base_url: str, *, audience: str = AUDIENCE) -> None:
        self._api = Sibling(http=http, base_url=base_url, service=SERVICE, audience=audience)

    async def providers(self, *, profile: str = "") -> tuple[Provider, ...]:
        """The probe. Answers per caller and per profile, so the statuses are about this turn.

        Without the profile the service checks the person's default one
        (Web-search-api/app/api/deps.py `get_caller`), while every search, open and summarise
        runs under the turn's. A session under `work` could be told "a research model is
        connected" because `personal` had one, and then fail on its first search.
        """
        payload = await self._api.send("GET", "/v1/models", profile=profile)
        return tuple(
            Provider(
                name=text(row, "name"),
                status=text(row, "status"),
                detail=text(row, "detail"),
                model_count=number(row, "model_count"),
            )
            for row in rows(payload, "providers")
        )

    async def search(
        self, queries: Sequence[str], *, profile: str = "", max_results: int = DEFAULT_RESULTS
    ) -> tuple[Findings, ...]:
        """Search, summarised, with the page text left where it was.

        `fetch_pages` is deliberately not offered: a search that also scrapes returns the
        full text of every result, and the one thing this projection is for is that the full
        text does not come back this way. Opening a page is `scrape`, which says so.
        """
        body = {
            "queries": [{"query": query, "max_results": max_results} for query in queries],
            "summarize": True,
        }
        payload = await self._api.send(
            "POST", "/v1/search", body=body, profile=profile, timeout_seconds=WORK_TIMEOUT_SECONDS
        )
        return tuple(_findings(row) for row in rows(payload, "results"))

    async def scrape(self, urls: Sequence[str], *, profile: str = "") -> Reading:
        """Fetch pages, summarised together, keeping each page's text on its own article."""
        body = {"urls": list(urls), "summarize": True, "summarize_together": True}
        payload = await self._api.send(
            "POST", "/v1/scrape", body=body, profile=profile, timeout_seconds=WORK_TIMEOUT_SECONDS
        )
        return Reading(
            articles=tuple(_article(row) for row in rows(payload, "results")),
            summary=_summary(nested(payload, "summary")),
        )

    async def summarize(self, body: str, *, topic: str = "", profile: str = "") -> Summary:
        """Summarise text, which is how a long tool result becomes a short one."""
        payload = await self._api.send(
            "POST",
            "/v1/summarize",
            body=given(text=body, topic=topic or None),
            profile=profile,
            timeout_seconds=WORK_TIMEOUT_SECONDS,
        )
        return _summary(nested(payload, "summary")) or Summary()


def _summary(payload: Any) -> Summary | None:
    """A summary, or `None` when the service did not make one.

    The notice is built here rather than by the caller because the three fields it is built
    from are the service's and would otherwise have to be carried through the projection
    just so that somebody else could phrase them.
    """
    if not isinstance(payload, dict):
        return None
    submitted, original = number(payload, "chars_submitted"), number(payload, "original_chars")
    truncated = flag(payload, "truncated")
    notice = f"summarised {submitted} of {original} characters" if truncated else ""
    return Summary(
        executive_summary=text(payload, "executive_summary"),
        key_points=tuple(str(point) for point in rows(payload, "key_points")),
        truncated=truncated,
        notice=notice,
    )


def _findings(row: Any) -> Findings:
    """One query's outcome, with every result reduced to a title, a link and a rank."""
    return Findings(
        query=text(row, "query"),
        status=text(row, "status", "ok"),
        hits=tuple(
            Hit(title=text(hit, "title"), url=text(hit, "url"), rank=number(hit, "rank"))
            for hit in rows(row, "results")
        ),
        summary=_summary(nested(row, "summary")),
        detail=text(nested(row, "error"), "detail"),
    )


def _article(row: Any) -> Article:
    """One fetched page: the citable part, and the text, kept apart."""
    page = nested(row, "page")
    return Article(
        page=Page(
            url=text(row, "url"),
            final_url=text(page, "final_url"),
            title=text(page, "title"),
            word_count=number(page, "word_count"),
            status=text(row, "status", "ok"),
            detail=text(nested(row, "error"), "detail"),
        ),
        text=text(page, "text"),
    )


class FakeSearchClient:
    """An in-memory web, so a research pack's tests need no network and no model.

    Seeded per query and per URL. `asked` records the queries, because "did it search twice
    for the same thing" is a question about the agent rather than about the web.
    """

    def __init__(self) -> None:
        self.catalogue: dict[str, Findings] = {}
        self.library: dict[str, Article] = {}
        self.known: tuple[Provider, ...] = (Provider(name="fake", status=AVAILABLE),)
        self.summary = Summary(executive_summary="a summary")
        self.asked: list[str] = []
        self.opened: list[str] = []
        self.profiles: list[str] = []
        """Which profile each call carried. Invisible in an answer, wrong in real bugs."""

    def seed(self, findings: Findings) -> None:
        """Make one query answerable."""
        self.catalogue[findings.query] = findings

    def stock(self, article: Article) -> None:
        """Make one URL fetchable."""
        self.library[article.page.url] = article

    def offer(self, providers: Iterable[Provider]) -> None:
        """Say which providers this person can use."""
        self.known = tuple(providers)

    async def providers(self, *, profile: str = "") -> tuple[Provider, ...]:
        """Whatever the test said about this person's providers."""
        self.profiles.append(profile)
        return self.known

    async def search(
        self, queries: Sequence[str], *, profile: str = "", max_results: int = DEFAULT_RESULTS
    ) -> tuple[Findings, ...]:
        """One result set per query, capped at `max_results` hits, in the order asked."""
        self.profiles.append(profile)
        self.asked.extend(queries)
        found: list[Findings] = []
        for query in queries:
            seeded = self.catalogue.get(query, Findings(query=query))
            found.append(
                Findings(
                    query=seeded.query,
                    status=seeded.status,
                    hits=seeded.hits[:max_results],
                    summary=seeded.summary,
                    detail=seeded.detail,
                )
            )
        return tuple(found)

    async def scrape(self, urls: Sequence[str], *, profile: str = "") -> Reading:
        """The seeded article for each stocked URL, and a failed row for each of the rest.

        A failed row rather than no row, because that is what the service answers: one result
        per URL asked for, whether or not it could be fetched.
        """
        self.profiles.append(profile)
        self.opened.extend(urls)
        articles = tuple(self.library.get(url) or _unfetched(url) for url in urls)
        return Reading(articles=articles, summary=self.summary)

    async def summarize(self, body: str, *, topic: str = "", profile: str = "") -> Summary:
        """The fixed summary, with the topic recorded so a test can see it travelled."""
        self.profiles.append(profile)
        self.asked.append(topic or body[:40])
        return self.summary


def _unfetched(url: str) -> Article:
    """What the service answers for a URL whose origin said 404."""
    return Article(page=Page(url=url, status="error", detail=f"{url} responded 404."))


if TYPE_CHECKING:

    def _satisfies(real: HttpSearchClient, fake: FakeSearchClient) -> tuple[SearchClient, ...]:
        """Static proof that both implementations satisfy the seam."""
        return (real, fake)


__all__ = [
    "AUDIENCE",
    "AVAILABLE",
    "DEFAULT_RESULTS",
    "NOT_CONFIGURED",
    "SERVICE",
    "Article",
    "FakeSearchClient",
    "Findings",
    "Hit",
    "HttpSearchClient",
    "Page",
    "Provider",
    "Reading",
    "SearchClient",
    "Summary",
]
