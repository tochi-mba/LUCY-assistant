"""Web research with bounded projections instead of raw pages in model context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from weftai.operation import define_operation
from weftai.schema import ref
from weftai.schema.spec import integer_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.search import (
    AUDIENCE,
    DEFAULT_RESULTS,
    NOT_CONFIGURED,
    HttpSearchClient,
)
from lucy_api.context.types import Trust
from lucy_api.packs.base import Availability, Permission, SetupPlan, SetupStep, State
from lucy_api.packs.collections import HIT
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.search import Page, SearchClient, Summary
    from lucy_api.packs.context import PackContext


HIT_REFERENCE = (
    'A result an earlier research.search returned, by reference: "$found[2]" for one. '
    '"$found" opens the first three.'
)
URL = (
    "An address the person gave, written out -- never a reference. To open a result a search "
    "found, give it as `hit`."
)
LOOKING_FOR = (
    "What you opened it to find: a figure, a date, a version, a claim to check. The summary "
    "keeps it rather than a general account of the page."
)
MAX_OPENED = 3
"""The most pages one open reads. A reference to a whole search is a list, and reading every
result of it would cost a fetch and a summary each for pages nobody chose."""
NOT_A_REFERENCE = (
    "`url` takes an address, not a reference; {url!r} looks like one. To open a result an "
    'earlier search found, give it as `hit`: {{"hit": "{url}"}}.'
)
EITHER = "Give `hit` or `url`, not both."
OPEN_WHAT = "Name what to open: `hit` for a search result, or `url` for an address."
NOTHING_FOUND = "The referenced search found no result to open; search again first."
TOO_MANY = "opened the first {opened} of the {found} results referenced; open the rest by index"


class ResearchInputError(ValueError):
    """An open the model can fix, said in a sentence it can act on."""


class ResearchPack:
    """Search, open and summarize without copying fetched page bodies into results."""

    id = "research"
    title = "Research"
    summary = "Search the web, open sources and summarize material with citations."

    def __init__(
        self,
        base_url: str,
        *,
        audience: str = AUDIENCE,
        client: SearchClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return ()

    def result_trust(self, operation: str, data: object) -> Trust:
        """Pages and search results: anyone can write one."""
        del operation, data
        return Trust.untrusted

    def setup(self) -> SetupPlan | None:
        return SetupPlan(
            summary="Connect a model provider for research summaries.",
            steps=(
                SetupStep(
                    id="provider",
                    kind="credential",
                    title="Connect a research model",
                    description="Store the provider credential in Keyring, never in chat.",
                ),
            ),
        )

    async def probe(self, context: PackContext) -> Availability:
        try:
            providers = await self._client(context).providers(profile=context.profile)
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(state=State.unavailable, detail="research could not be reached")
        if any(provider.usable for provider in providers):
            return Availability(state=State.ready, detail="a research model is connected")
        if providers and any(provider.status == NOT_CONFIGURED for provider in providers):
            return Availability(
                state=State.not_connected,
                detail="connect a model provider to make research available",
            )
        if providers:
            return Availability(state=State.unavailable, detail="no research model is usable")
        return Availability(state=State.not_configured, detail="no research provider is deployed")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        del context
        return (
            define_operation(
                {
                    "name": "research.search",
                    "description": (
                        "Search the web for one question and return ranked, citable sources "
                        "plus a bounded summary; page text stays out of context. Several "
                        "searches in one plan run together."
                    ),
                    "input": object_schema(
                        {
                            "query": string_schema().describe("The question or search terms."),
                            "limit": integer_schema().optional(),
                        }
                    ),
                    "output": HIT,
                    "effects": "read",
                    "run": self._search,
                }
            ),
            define_operation(
                {
                    "name": "research.open",
                    "description": (
                        "Read a page and return its citation metadata and a summary, not "
                        "the unbounded page body. The summary is all you keep of it, so say "
                        "in `looking_for` what you opened it to find."
                    ),
                    "input": object_schema(
                        {
                            "hit": ref(HIT, description=HIT_REFERENCE).optional(),
                            "url": string_schema().describe(URL).optional(),
                            "looking_for": string_schema().describe(LOOKING_FOR).optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._open,
                }
            ),
            define_operation(
                {
                    "name": "research.summarize",
                    "description": "Summarize text already available in this conversation.",
                    "input": object_schema(
                        {
                            "body": string_schema().describe("Text to summarize."),
                            "topic": string_schema().optional(),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._summarize,
                }
            ),
        )

    def _client(self, context: PackContext) -> SearchClient:
        return self._override or HttpSearchClient(
            context.http, self.base_url, audience=self.audience
        )

    async def _search(self, run: RunContext[PackContext]) -> list[dict[str, Any]]:
        limit = _search_limit(run)
        query = str(run.input.get("query") or "")
        findings = await self._client(run.ctx).search(
            (query,), profile=run.ctx.profile, max_results=limit
        )
        hits: list[dict[str, Any]] = []
        for result in findings:
            summary = _summary(result.summary)
            for index, hit in enumerate(result.hits):
                row: dict[str, Any] = {
                    "title": hit.title,
                    "url": hit.url,
                    "source": urlsplit(hit.url).hostname or "",
                    "rank": hit.rank,
                    "snippet": hit.snippet,
                }
                if index == 0 and summary:
                    # One summary covers the whole query, so it rides on the first row once.
                    # Copied onto every row it was paid for five times at the default count,
                    # and again on each later round the result stayed in the window. The
                    # key says what it covers, so it is not read as being about hit 1.
                    row["summary_of_all_results"] = summary
                hits.append(row)
            if result.detail:
                run.notice(f"research for {result.query!r}: {result.detail}")
        return hits

    async def _open(self, run: RunContext[PackContext]) -> dict[str, Any]:
        urls = _addresses(run)
        reading = await self._client(run.ctx).scrape(
            urls,
            profile=run.ctx.profile,
            looking_for=" ".join(str(run.input.get("looking_for") or "").split()),
        )
        pages = reading.pages()
        for page in pages:
            if not page.fetched:
                run.notice(f"could not open {page.url}: {page.detail}")
        return {"pages": [_page(page) for page in pages], "summary": _summary(reading.summary)}

    async def _summarize(self, run: RunContext[PackContext]) -> dict[str, Any]:
        summary = await self._client(run.ctx).summarize(
            str(run.input.get("body") or ""),
            topic=str(run.input.get("topic") or ""),
            profile=run.ctx.profile,
        )
        return _summary(summary) or {}


def _addresses(run: RunContext[PackContext]) -> tuple[str, ...]:
    """The pages an open names: the results a reference resolved to, or the one address."""
    url = str(run.input.get("url") or "").strip()
    picked = run.input.get("hit")
    if picked is not None and url:
        raise ResearchInputError(EITHER)
    if url.startswith("$"):
        raise ResearchInputError(NOT_A_REFERENCE.format(url=url))
    if picked is None:
        if not url:
            raise ResearchInputError(OPEN_WHAT)
        return (url,)
    found = tuple(
        str(item["url"]) for item in picked.items if isinstance(item, dict) and item.get("url")
    )
    if not found:
        raise ResearchInputError(NOTHING_FOUND)
    if len(found) > MAX_OPENED:
        run.notice(TOO_MANY.format(opened=MAX_OPENED, found=len(found)))
    return found[:MAX_OPENED]


def _search_limit(run: RunContext[PackContext]) -> int:
    raw = run.input.get("limit")
    if raw is None:
        raw = run.ctx.defaults.get("research.limit", DEFAULT_RESULTS)
    try:
        return max(1, min(int(raw), 20))
    except (TypeError, ValueError):
        return DEFAULT_RESULTS


def _page(page: Page) -> dict[str, Any]:
    """Citation metadata, and for a page that was never read, the service's reason why."""
    projected: dict[str, Any] = {
        "url": page.url,
        "final_url": page.final_url,
        "title": page.title,
        "word_count": page.word_count,
        "status": page.status,
    }
    if page.detail:
        projected["error"] = page.detail
    return projected


def _summary(summary: Summary | None) -> dict[str, Any] | None:
    """A summary as the model reads it: what it says, and a notice only when there is one.

    `truncated: false, notice: ""` on every summary was text the model paid for and learned
    nothing from. An empty summary is no summary.
    """
    if summary is None or not summary.executive_summary:
        return None
    projected: dict[str, Any] = {
        "executive_summary": summary.executive_summary,
        "key_points": list(summary.key_points),
    }
    if summary.truncated:
        projected["truncated"] = True
    if summary.notice:
        projected["notice"] = summary.notice
    return projected


__all__ = ["ResearchPack"]
