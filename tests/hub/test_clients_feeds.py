"""The shared prompt-feed client: one path, the profile on the request, the document decoded."""

from __future__ import annotations

from lucy_api.clients.feeds import PATH, HttpFeeds
from lucy_api.clients.testing import Answer, FakeHttp
from lucy_api.clients.transport import PROFILE_HEADER, Sibling
from lucy_api.context.feeds import FeedRequest, Volatility


def client(answer: Answer) -> tuple[HttpFeeds, FakeHttp]:
    http = FakeHttp(answer)
    sibling = Sibling(
        http=http, base_url="http://notes.test", service="persona", audience="persona-api"
    )
    return HttpFeeds(sibling, name="persona"), http


async def test_a_feed_read_asks_for_the_profile_and_the_standard_path() -> None:
    feeds, http = client(
        Answer(
            body={
                "feeds": [
                    {
                        "id": "persona",
                        "title": "Who you are",
                        "entries": [{"key": "notes", "line": "prefers tea"}],
                    }
                ]
            }
        )
    )

    parsed = await feeds.fetch(FeedRequest(profile="personal"))

    assert parsed[0].id == "persona"
    assert parsed[0].volatility is Volatility.standing
    assert parsed[0].lines == ("prefers tea",)
    assert http.last.url.endswith(PATH)
    assert http.last.headers[PROFILE_HEADER] == "personal"
    assert http.last.audience == "persona-api"
    assert "tea" in str(parsed[0].lines)
