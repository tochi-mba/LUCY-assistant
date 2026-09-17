"""The standard prompt-feed document, fetched from one sibling.

Every capability that wants a line in the prompt publishes the same JSON at
``GET /v1/prompt/feeds``. Lucy does not learn a new client per service for this: one
shape, one path, one decode. The sibling's product name (``persona``, ``music``) is
``HttpFeeds.name``, never a host or a port.
"""

from __future__ import annotations

from lucy_api.clients.transport import Sibling, given
from lucy_api.context.feeds import Feed, FeedRequest, parse_document

PATH = "/v1/prompt/feeds"


class HttpFeeds:
    """One sibling's feed, over the same HTTP seam every other client uses."""

    def __init__(self, sibling: Sibling, *, name: str, path: str = PATH) -> None:
        self.name = name
        self.path = path
        self._api = sibling

    async def fetch(self, request: FeedRequest) -> tuple[Feed, ...]:
        body = await self._api.send(
            "GET",
            self.path,
            params=given(profile=request.profile or None),
            profile=request.profile,
        )
        return parse_document(body)


__all__ = ["PATH", "HttpFeeds"]
