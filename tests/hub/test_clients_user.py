"""User-api's pinned set is projected; account ids and sensitive rows never leave."""

from __future__ import annotations

import pytest

from lucy_api.clients.errors import AbsentError
from lucy_api.clients.testing import Answer, FakeHttp, problem
from lucy_api.clients.transport import PROFILE_HEADER
from lucy_api.clients.user import PATH, AccountFact, HttpUserClient, as_dict


def client(answer: Answer) -> tuple[HttpUserClient, FakeHttp]:
    http = FakeHttp(answer)
    return HttpUserClient(http, "http://account.test"), http


async def test_pinned_asks_for_the_always_load_set_under_the_bare_audience() -> None:
    reader, http = client(
        Answer(
            body={
                "entries": [
                    {
                        "entry_id": "ent_1",
                        "entry_type": "field",
                        "key": "preferred_name",
                        "value": "Sam",
                        "source": "stated",
                        "asserted_by": "user",
                        "pinned": True,
                        "account_id": "acct_secret",
                        "updated_at": "2026-03-02T18:40:00+00:00",
                    },
                    {
                        "entry_id": "ent_2",
                        "entry_type": "note",
                        "body": "Ask before summarising.",
                        "source": "inferred",
                        "asserted_by": "user",
                        "pinned": True,
                    },
                ]
            }
        )
    )

    facts = await reader.pinned(profile="personal")

    assert [fact.key for fact in facts] == ["preferred_name", ""]
    assert facts[0].line == 'preferred_name: "Sam"'
    assert facts[1].line == "Ask before summarising."
    assert "acct_secret" not in as_dict(facts[0]).values()
    assert "account_id" not in as_dict(facts[0])
    assert http.last.url.endswith(PATH)
    assert http.last.params == {"pinned": True}
    assert http.last.audience == "user"
    assert http.last.headers[PROFILE_HEADER] == "personal"


async def test_sensitive_unpinned_empty_and_malformed_rows_are_dropped() -> None:
    reader, _http = client(
        Answer(
            body={
                "entries": [
                    "not-a-row",
                    {
                        "entry_id": "ent_s",
                        "entry_type": "field",
                        "key": "ssn",
                        "value": "000",
                        "sensitivity": "sensitive",
                        "pinned": True,
                    },
                    {
                        "entry_id": "ent_u",
                        "entry_type": "field",
                        "key": "nickname",
                        "value": "Sam",
                        "pinned": False,
                    },
                    {
                        "entry_id": "ent_e",
                        "entry_type": "note",
                        "body": "   ",
                        "pinned": True,
                    },
                    {
                        "entry_id": "ent_k",
                        "value": True,
                        "pinned": True,
                    },
                ]
            }
        )
    )

    facts = await reader.pinned()

    assert facts == (
        AccountFact(id="ent_k", kind="field", key="", line="true", source="", asserted_by=""),
    )


async def test_an_empty_page_is_no_facts_not_a_failure() -> None:
    reader, _http = client(Answer(body={"entries": []}))
    assert await reader.pinned() == ()


async def test_a_missing_account_is_absence() -> None:
    reader, _http = client(problem(404, code="not-found"))
    with pytest.raises(AbsentError):
        await reader.pinned()
