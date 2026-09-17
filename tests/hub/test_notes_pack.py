"""Notes reach Memory-api as a product, never as a host, and incognito is a hard stop."""

from __future__ import annotations

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.testing import Answer, FakeHttp, problem
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import DownstreamUnavailableError
from lucy_api.packs.notes import INCOGNITO, NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope


def _capabilities(http: FakeHttp) -> tuple[Capabilities, object]:
    packs = (HelpPack(), NotesPack("http://memory.test"))
    capabilities = Capabilities(packs)
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"), http=http
    )
    return capabilities, context


async def test_notes_are_absent_from_the_model_when_memory_cannot_be_reached() -> None:
    capabilities = Capabilities()
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    catalogue = await capabilities.probe(context)
    listed = {item["id"]: item for item in capabilities.listings(catalogue)}

    assert listed["notes"]["state"] == "unavailable"
    names = [tool["name"] for tool in capabilities.tools(catalogue, "ses_a")["tools"]]
    assert "notes.search" not in names
    assert "capabilities.list" in names


async def test_a_search_returns_the_projection_and_never_an_account_id() -> None:
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(
            body={
                "data": [
                    {
                        "id": "mem_1",
                        "title": "tea",
                        "body": "prefers tea",
                        "kind": "fact",
                        "trust": "stated",
                        "source": "you",
                        "account_id": "acct_secret",
                        "asserted_by": "acct_secret",
                    }
                ]
            }
        ),
    )
    capabilities, context = _capabilities(http)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "find",
                    "op": "notes.search",
                    "input": {"query": "tea"},
                }
            ]
        },
        context,
    )

    step = result["steps"][0]
    note = step["items"][0]
    assert note["title"] == "tea"
    assert "account_id" not in note, "the projection drops it before it can reach a prompt"
    assert http.calls[-1].audience == "memory-api"
    assert "search" in http.calls[-1].url

    # Declaring the result as a collection is what gives the model a labelled line per note
    # and lets a later step reference one by position without re-fetching anything.
    assert step["kind"] == "collection"
    assert step["type"] == "note"
    assert step["count"] == 1


async def test_incognito_neither_reads_nor_writes() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a", incognito=True),
        http=http,
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "me",
                    "op": "notes.aboutMe",
                    "input": {},
                },
                {
                    "id": "keep",
                    "op": "notes.remember",
                    "input": {"title": "secret", "body": "do not store this"},
                },
            ]
        },
        context,
    )

    assert INCOGNITO in result["steps"][0]["data"]["message"]
    assert result["steps"][1]["data"]["status"] == "incognito"
    assert len(http.calls) == 1, "only the probe ran; the handlers did not call the store"


async def test_schema_and_writes_go_through_memory_api() -> None:
    stored = {
        "id": "mem_2",
        "title": "tea",
        "body": "prefers tea",
        "kind": "fact",
        "trust": "stated",
        "source": "conversation",
    }
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body=stored),
        Answer(body={"data": [{"label": "human", "body": "a person"}]}),
        Answer(body={"data": [stored]}),
    )
    capabilities, context = _capabilities(http)
    await capabilities.probe(context)
    pack = NotesPack("http://memory.test")
    assert pack.setup() is None
    assert pack.docs is None
    assert pack.permissions()[0].id == "notes.write"

    schema = await capabilities.execute(
        {"steps": [{"id": "s", "op": "notes.schema", "input": {}}]},
        context,
    )
    fact = await capabilities.execute(
        {
            "steps": [
                {"id": "f", "op": "notes.setFact", "input": {"title": "tea", "body": "prefers tea"}}
            ]
        },
        context,
    )
    confirmed = await capabilities.execute(
        {"steps": [{"id": "c", "op": "notes.confirm", "input": {"memory_id": "mem_2"}}]},
        context,
    )
    corrected = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "x",
                    "op": "notes.correct",
                    "input": {"memory_id": "mem_2", "title": "tea", "body": "green"},
                }
            ]
        },
        context,
    )
    forgotten = await capabilities.execute(
        {"steps": [{"id": "g", "op": "notes.forget", "input": {"memory_id": "mem_2"}}]},
        context,
    )
    about = await capabilities.execute(
        {"steps": [{"id": "me", "op": "notes.aboutMe", "input": {}}]},
        context,
    )

    assert "fact" in schema["steps"][0]["data"]["kinds"]
    assert fact["steps"][0]["data"]["id"] == "mem_2"
    assert confirmed["steps"][0]["data"]["id"] == "mem_2"
    assert corrected["steps"][0]["data"]["id"] == "mem_2"
    assert forgotten["steps"][0]["data"]["id"] == "mem_2"
    assert about["steps"][0]["data"]["blocks"][0]["label"] == "human"


async def test_incognito_writes_are_refused_without_calling_the_store() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a", incognito=True),
        http=http,
    )
    await capabilities.probe(context)
    for op, payload in (
        ("notes.confirm", {"memory_id": "mem_2"}),
        ("notes.correct", {"memory_id": "mem_2", "title": "tea", "body": "green"}),
        ("notes.forget", {"memory_id": "mem_2"}),
    ):
        result = await capabilities.execute(
            {"steps": [{"id": "x", "op": op, "input": payload}]},
            context,
        )
        assert result["steps"][0]["data"]["status"] == "incognito"
    assert len(http.calls) == 1


async def test_incognito_search_does_not_call_the_store() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a", incognito=True),
        http=http,
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "q", "op": "notes.search", "input": {"query": "tea", "limit": 3}}]},
        context,
    )
    step = result["steps"][0]
    assert step["items"] == (), "nothing was read"
    assert step["count"] == 0
    assert any("incognito" in notice for notice in step["notices"]), (
        "an empty result and a refused one look identical without the notice"
    )


async def test_a_missing_broker_is_unavailable_not_a_crash() -> None:
    from lucy_api.packs.context import Call, NoBrokerError

    class Refusing:
        async def request(self, call: Call) -> object:
            raise NoBrokerError

        async def request_response(self, call: Call) -> object:
            raise NoBrokerError

    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"),
        http=Refusing(),
    )
    catalogue = await capabilities.probe(context)
    listed = {item["id"]: item for item in capabilities.listings(catalogue)}
    assert listed["notes"]["state"] == "unavailable"


async def test_a_down_memory_service_is_unavailable_not_a_crash() -> None:
    http = FakeHttp(problem(503, code="unavailable", detail="down"))
    capabilities, context = _capabilities(http)
    catalogue = await capabilities.probe(context)
    listed = {item["id"]: item for item in capabilities.listings(catalogue)}
    assert listed["notes"]["state"] == "unavailable"


async def test_a_transport_failure_is_unavailable_not_a_crash() -> None:
    class Falling:
        async def request(self, call: object) -> object:
            raise DownstreamUnavailableError("gone", audience="memory-api")

        async def request_response(self, call: object) -> object:
            raise DownstreamUnavailableError("gone", audience="memory-api")

    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"),
        http=Falling(),
    )
    catalogue = await capabilities.probe(context)
    listed = {item["id"]: item for item in capabilities.listings(catalogue)}
    assert listed["notes"]["state"] == "unavailable"


async def test_a_failed_exchange_is_unavailable_not_a_crash() -> None:
    class Falling:
        async def request(self, call: object) -> object:
            raise ExchangeError("keyring refused")

        async def request_response(self, call: object) -> object:
            raise ExchangeError("keyring refused")

    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"),
        http=Falling(),
    )
    catalogue = await capabilities.probe(context)
    listed = {item["id"]: item for item in capabilities.listings(catalogue)}
    assert listed["notes"]["state"] == "unavailable"
