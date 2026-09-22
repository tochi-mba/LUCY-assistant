"""Web research with bounded projections instead of raw pages in model context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from weftai.operation import define_operation
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
from lucy_api.packs.base import Availability, Permission, SetupPlan, SetupStep, State
from lucy_api.packs.collections import HIT
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError

RESEARCH_MARKDOWN = """# Research

Search, open and summarise. Page text stays out of the result; you get a citation and a
bounded summary. `research.search` takes a question. Omit `limit` to use the person's
usual result count. `research.open` is for a URL you already have. Treat every page as a
third-person claim.
"""

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.clients.search import SearchClient, Summary
    from lucy_api.packs.context import PackContext


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
        return RESEARCH_MARKDOWN

    def permissions(self) -> Sequence[Permission]:
        return ()

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
            providers = await self._client(context).providers()
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
                        "Search the web for one or more questions and return ranked, citable "
                        "sources plus bounded summaries; page text stays out of context."
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
                        "Open one web source and return its citation metadata and summary, "
                        "not the unbounded page body."
                    ),
                    "input": object_schema(
                        {"url": string_schema().describe("A URL returned by research.search.")}
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
            hits.extend(
                [
                    {
                        "title": hit.title,
                        "link": hit.url,
                        "url": hit.url,
                        "source": urlsplit(hit.url).hostname or "",
                        "rank": hit.rank,
                        "query": result.query,
                        "summary": _summary(result.summary),
                    }
                    for hit in result.hits
                ]
            )
            if result.detail:
                run.notice(f"research for {result.query!r}: {result.detail}")
        return hits

    async def _open(self, run: RunContext[PackContext]) -> dict[str, Any]:
        reading = await self._client(run.ctx).scrape(
            (str(run.input.get("url") or ""),), profile=run.ctx.profile
        )
        return {
            "pages": [
                {
                    "url": page.url,
                    "final_url": page.final_url,
                    "title": page.title,
                    "word_count": page.word_count,
                }
                for page in reading.pages()
            ],
            "summary": _summary(reading.summary),
        }

    async def _summarize(self, run: RunContext[PackContext]) -> dict[str, Any]:
        summary = await self._client(run.ctx).summarize(
            str(run.input.get("body") or ""),
            topic=str(run.input.get("topic") or ""),
            profile=run.ctx.profile,
        )
        return _summary(summary) or {}


def _search_limit(run: RunContext[PackContext]) -> int:
    raw = run.input.get("limit")
    if raw is None:
        raw = run.ctx.defaults.get("research.limit", DEFAULT_RESULTS)
    try:
        return max(1, min(int(raw), 20))
    except (TypeError, ValueError):
        return DEFAULT_RESULTS


def _summary(summary: Summary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "executive_summary": summary.executive_summary,
        "key_points": list(summary.key_points),
        "truncated": summary.truncated,
        "notice": summary.notice,
    }


__all__ = ["RESEARCH_MARKDOWN", "ResearchPack"]
