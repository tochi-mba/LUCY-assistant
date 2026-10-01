"""Notes reach Memory-api as a product, never as a host, and incognito is a hard stop."""

from __future__ import annotations

import pytest

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.testing import Answer, FakeHttp, problem
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import DownstreamUnavailableError
from lucy_api.packs.notes import NotesPack
from lucy_api.packs.service import Capabilities
from lucy_api.permissions.gate import INCOGNITO, Grant
from lucy_api.prompt.docs import capability_doc
from lucy_api.sessions.scope import SessionScope
from lucy_api.settings.policy import TurnPolicy


def _capabilities(
    http: FakeHttp,
    *,
    permission_mode: str = "ask",
    incognito: bool = False,
    user_base_url: str = "",
) -> tuple[Capabilities, object]:
    packs = (HelpPack(), NotesPack("http://memory.test", user_base_url=user_base_url))
    capabilities = Capabilities(packs)
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a",
            profile="personal",
            session_id="ses_a",
            permission_mode=permission_mode,
            incognito=incognito,
        ),
        http=http,
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
    capabilities, context = _capabilities(http, permission_mode="auto", incognito=True)
    await capabilities.probe(context)
    read = await capabilities.execute(
        {"steps": [{"id": "me", "op": "notes.aboutMe", "input": {}}]}, context
    )
    write = await capabilities.execute(
        {
            "steps": [
                {
                    "id": "keep",
                    "op": "notes.remember",
                    "input": {"title": "secret", "body": "do not store this"},
                }
            ]
        },
        context,
    )

    assert INCOGNITO in read["steps"][0]["data"]["message"]
    assert write["steps"] == []
    assert write["issues"][0]["code"] == "permission_denied"
    assert len(http.calls) == 1, (
        "only the probe ran; neither the handler nor the gate called the store"
    )


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
        Answer(body=stored),
        Answer(body={"data": [{"label": "human", "body": "a person"}]}),
        Answer(body={"data": [stored]}),
    )
    capabilities, context = _capabilities(http, permission_mode="auto")
    await capabilities.probe(context)
    pack = NotesPack("http://memory.test")
    assert pack.setup() is None
    assert pack.docs == capability_doc("notes")
    assert pack.permissions()[0].id == "notes.write"
    assert pack.permissions()[1].id == "notes.erase"
    assert pack.permissions()[1].covers == ("notes.forget",)
    context.grants["notes.erase"] = Grant("notes.erase", "allow", "*")

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
    assert "account" in schema["steps"][0]["data"]["sections"]
    assert fact["steps"][0]["data"]["id"] == "mem_2"
    assert confirmed["steps"][0]["data"]["id"] == "mem_2"
    assert corrected["steps"][0]["data"]["id"] == "mem_2"
    assert forgotten["steps"][0]["data"]["id"] == "mem_2"
    assert about["steps"][0]["data"]["blocks"][0]["label"] == "human"
    assert about["steps"][0]["data"]["account"] == []


async def test_about_me_keeps_account_pins_in_a_separate_list() -> None:
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": [{"label": "human", "body": "a person"}]}),
        Answer(
            body={
                "data": [
                    {
                        "id": "mem_1",
                        "title": "tea",
                        "body": "prefers tea",
                        "kind": "fact",
                        "trust": "stated",
                    }
                ]
            }
        ),
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
                    }
                ]
            }
        ),
    )
    capabilities, context = _capabilities(
        http, permission_mode="auto", user_base_url="http://account.test"
    )
    await capabilities.probe(context)
    about = await capabilities.execute(
        {"steps": [{"id": "me", "op": "notes.aboutMe", "input": {}}]},
        context,
    )

    data = about["steps"][0]["data"]
    assert data["blocks"][0]["label"] == "human"
    assert data["facts"][0]["id"] == "mem_1"
    assert data["account"][0]["key"] == "preferred_name"
    assert "account_id" not in data["account"][0]
    assert "acct_secret" not in str(data)
    assert http.calls[-1].url.endswith("/v1/user/entries")
    assert http.calls[-1].audience == "user"


async def test_an_account_outage_does_not_blank_the_memories() -> None:
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": [{"label": "human", "body": "a person"}]}),
        Answer(body={"data": [{"id": "mem_1", "title": "tea", "body": "prefers tea"}]}),
        problem(503, code="unavailable"),
    )
    capabilities, context = _capabilities(
        http, permission_mode="auto", user_base_url="http://account.test"
    )
    await capabilities.probe(context)
    about = await capabilities.execute(
        {"steps": [{"id": "me", "op": "notes.aboutMe", "input": {}}]},
        context,
    )

    data = about["steps"][0]["data"]
    assert data["facts"][0]["id"] == "mem_1"
    assert data["account"] == []


async def test_search_does_not_call_the_account_store() -> None:
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": [{"id": "mem_1", "title": "tea", "body": "prefers tea"}]}),
    )
    capabilities, context = _capabilities(
        http, permission_mode="auto", user_base_url="http://account.test"
    )
    await capabilities.probe(context)
    await capabilities.execute(
        {"steps": [{"id": "find", "op": "notes.search", "input": {"query": "tea"}}]},
        context,
    )

    assert all("/v1/user/" not in call.url for call in http.calls)
    assert http.calls[-1].url.endswith("/v1/internal/memory/search")


@pytest.mark.parametrize(
    ("op", "arguments"),
    [("notes.search", {"query": "tour dates"}), ("notes.aboutMe", {})],
)
async def test_reading_notes_asks_for_this_sessions_episodes(
    op: str, arguments: dict[str, str]
) -> None:
    """`notes.remember` tells the model an episode "stays with this session". Both ways of
    reading it back have to say which session that is, or the store compares against NULL
    and the episode is never returned -- not even to the session that wrote it."""
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(body={"data": []}),
        Answer(body={"data": []}),
    )
    capabilities, context = _capabilities(http, permission_mode="auto")
    await capabilities.probe(context)
    await capabilities.execute({"steps": [{"id": "r", "op": op, "input": arguments}]}, context)

    searched = [call for call in http.calls if call.url.endswith("/v1/internal/memory/search")]
    assert searched
    assert searched[-1].params is not None
    assert searched[-1].params["session_id"] == "ses_a"


async def test_incognito_writes_are_denied_without_calling_the_store() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="auto", incognito=True)
    context.grants["notes.erase"] = Grant("notes.erase", "allow", "*")
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
        assert result["issues"][0]["code"] == "permission_denied", op
        assert result["issues"][0]["message"] == INCOGNITO, op
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


async def test_opening_a_topic_returns_its_notes_and_never_an_account_id() -> None:
    http = FakeHttp(
        Answer(body={"data": []}),
        Answer(
            body={
                "data": [
                    {
                        "id": "mem_1",
                        "title": "tea",
                        "body": "prefers tea",
                        "account_id": "secret",
                    }
                ]
            }
        ),
    )
    capabilities, context = _capabilities(http)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "open", "op": "notes.openTopic", "input": {"topic_id": "top_1"}}]},
        context,
    )

    note = result["steps"][0]["items"][0]
    assert note["title"] == "tea"
    assert "account_id" not in note
    assert http.calls[-1].url.endswith("/v1/internal/memory/topics/top_1")


async def test_opening_a_topic_in_incognito_reads_nothing() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities = Capabilities((HelpPack(), NotesPack("http://memory.test")))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a", incognito=True),
        http=http,
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "open", "op": "notes.openTopic", "input": {"topic_id": "top_1"}}]},
        context,
    )

    assert result["steps"][0]["items"] == ()
    assert len(http.calls) == 1


async def test_a_write_in_ask_mode_is_held_for_approval_not_run() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["steps"] == []
    assert result["issues"][0]["code"] == "permission_required"
    assert result["issues"][0]["permission"] == "notes.write"
    assert result["issues"][0]["operation"] == "notes.remember"
    assert len(http.calls) == 1


async def test_never_remembering_refuses_a_write_even_with_a_grant() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="auto")
    context.grants = {
        "notes.write": Grant(permission="notes.write", decision="allow", profile="personal")
    }
    context.policy = TurnPolicy(memory_write_policy="never")
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["issues"][0]["code"] == "permission_denied"
    assert "Remembering is off" in result["issues"][0]["message"]


async def test_an_incognito_session_denies_a_note_write_before_anyone_is_asked() -> None:
    """The bug, named: "remember that my favourite editor is helix" in an incognito session
    parked for approval on `notes.setFact`, the person approved it, and the step then met
    the refusal the handler holds. An approval for a write that cannot happen asks for
    nothing. The gate knows the session is incognito, and denies the write and the erase
    as it denies a `never` policy, so the model routes around it in the same round."""
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, incognito=True)
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}},
                {"id": "drop", "op": "notes.forget", "input": {"memory_id": "mem_1"}},
            ]
        },
        context,
    )

    assert [(item["code"], item["operation"]) for item in result["issues"]] == [
        ("permission_denied", "notes.remember"),
        ("permission_denied", "notes.forget"),
    ]
    assert {item["message"] for item in result["issues"]} == {INCOGNITO}
    assert len(http.calls) == 1
    assert len(http.calls) == 1


async def test_automatic_remembering_writes_in_ask_mode_without_a_grant() -> None:
    http = FakeHttp(Answer(body={"data": []}), Answer(status_code=201, body={"id": "mem_tea"}))
    capabilities, context = _capabilities(http)
    context.policy = TurnPolicy(memory_write_policy="automatic")
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["issues"] in (None, [], ())
    assert result["steps"][0]["status"] == "ok"


async def test_automatic_remembering_is_still_read_only_in_plan_mode() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="plan")
    context.policy = TurnPolicy(memory_write_policy="automatic")
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["issues"][0]["code"] == "permission_denied"
    assert "read-only" in result["issues"][0]["message"]


async def test_a_write_in_plan_mode_is_refused_rather_than_held_for_approval() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="plan")
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["issues"][0]["code"] == "permission_denied"
    assert result["steps"] == []
    assert len(http.calls) == 1


async def test_a_denied_grant_refuses_a_write_even_in_auto_mode() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="auto")
    context.grants = {
        "notes.write": Grant(
            permission="notes.write",
            decision="deny",
            profile="personal",
            instruction="Do not keep that.",
        )
    }
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["issues"][0]["code"] == "permission_denied"
    assert result["issues"][0]["message"] == "Do not keep that."
    assert len(http.calls) == 1


async def test_an_allow_grant_lets_a_write_run_in_ask_mode() -> None:
    http = FakeHttp(Answer(body={"data": []}), Answer(status_code=201, body={"id": "mem_tea"}))
    capabilities, context = _capabilities(http)
    context.grants = {
        "notes.write": Grant(permission="notes.write", decision="allow", profile="personal")
    }
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )

    assert result["issues"] in (None, [], ())
    assert result["steps"][0]["status"] == "ok"


async def test_an_account_grant_keyed_with_the_wildcard_still_allows_the_write() -> None:
    http = FakeHttp(Answer(body={"data": []}), Answer(status_code=201, body={"id": "mem_tea"}))
    capabilities, context = _capabilities(http)
    context.grants = {
        "*:notes.write": Grant(permission="notes.write", decision="allow", profile="*")
    }
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )
    assert result["steps"][0]["status"] == "ok"


async def test_a_deny_without_an_instruction_uses_the_permission_title() -> None:
    http = FakeHttp(Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="auto")
    context.grants = {
        "notes.write": Grant(permission="notes.write", decision="deny", profile="personal")
    }
    await capabilities.probe(context)
    result = await capabilities.execute(
        {
            "steps": [
                {"id": "keep", "op": "notes.remember", "input": {"title": "tea", "body": "green"}}
            ]
        },
        context,
    )
    assert result["issues"][0]["code"] == "permission_denied"
    assert "not allowed" in result["issues"][0]["message"]


# --- a note found and acted on by reference ---------------------------------------------------

FOUND = [
    {"id": "mem_1", "title": "coffee", "body": "black", "kind": "fact", "trust": "stated"},
    {"id": "mem_2", "title": "coffee", "body": "oat milk", "kind": "fact", "trust": "stated"},
]


def _found_and(step: dict[str, object]) -> dict[str, object]:
    return {
        "steps": [
            {"id": "found", "op": "notes.search", "input": {"query": "coffee"}},
            {"id": "act", **step},
        ]
    }


async def _held(*answers: Answer, plan: dict[str, object]) -> tuple[dict[str, object], FakeHttp]:
    http = FakeHttp(Answer(body={"data": []}), Answer(body={"data": FOUND}), *answers)
    capabilities, context = _capabilities(http, permission_mode="auto")
    context.grants["notes.erase"] = Grant("notes.erase", "allow", "*")
    context.grants["notes.write"] = Grant("notes.write", "allow", "*")
    await capabilities.probe(context)
    return await capabilities.execute(plan, context), http


def _forgot(http: FakeHttp) -> list[str]:
    return [call.url.rsplit("/", 2)[-2] for call in http.calls if call.url.endswith("/forget")]


async def test_what_a_search_found_is_forgotten_by_reference_and_each_note_reported() -> None:
    """The bug, named: asked to forget everything it knew about the person, the model found
    the notes and planned `notes.forget {"memory_id": "$found[1]"}`. `memory_id` is plain
    text, so the plan was refused, and there was no way to forget what had just been found."""
    result, http = await _held(
        Answer(body=FOUND[0]),
        Answer(body=FOUND[1]),
        plan=_found_and({"op": "notes.forget", "input": {"memory": "$found"}}),
    )
    data = result["steps"][1]["data"]
    assert [note["id"] for note in data["forgotten"]] == ["mem_1", "mem_2"]
    assert data["not_forgotten"] == []
    assert _forgot(http) == ["mem_1", "mem_2"]


async def test_a_forget_that_fails_part_way_says_which_notes_are_gone() -> None:
    result, _http = await _held(
        Answer(body=FOUND[0]),
        problem(404, code="not-found", detail="no such note"),
        plan=_found_and({"op": "notes.forget", "input": {"memory": "$found"}}),
    )
    data = result["steps"][1]["data"]
    assert [note["id"] for note in data["forgotten"]] == ["mem_1"]
    assert [row["memory_id"] for row in data["not_forgotten"]] == ["mem_2"]
    assert data["not_forgotten"][0]["reason"]


async def test_a_forget_that_fails_for_every_note_is_a_failed_step() -> None:
    result, _http = await _held(
        problem(503, detail="memory is down"),
        problem(503, detail="memory is down"),
        plan=_found_and({"op": "notes.forget", "input": {"memory": "$found"}}),
    )
    assert result["steps"][1]["status"] == "error"


async def test_one_found_note_is_corrected_or_confirmed_by_its_position() -> None:
    corrected = {**FOUND[1], "body": "oat milk, no sugar"}
    # A correction is two calls -- the note is read, then superseded -- and a confirm one.
    result, http = await _held(
        Answer(body=corrected),
        Answer(body=corrected),
        Answer(body=corrected),
        plan={
            "steps": [
                {"id": "found", "op": "notes.search", "input": {"query": "coffee"}},
                {
                    "id": "fix",
                    "op": "notes.correct",
                    "input": {
                        "memory": "$found[2]",
                        "title": "coffee",
                        "body": "oat milk, no sugar",
                    },
                },
                {"id": "vouch", "op": "notes.confirm", "input": {"memory": "$found[2]"}},
            ]
        },
    )
    assert [step["status"] for step in result["steps"]] == ["ok", "ok", "ok"]
    assert all("/mem_2" in call.url for call in http.calls[-3:])


@pytest.mark.parametrize(
    ("step", "said"),
    [
        (
            {"op": "notes.correct", "input": {"memory": "$found", "title": "t", "body": "b"}},
            '`memory` names 2 notes and notes.correct takes one: pick it with "$step[n]".',
        ),
        (
            {"op": "notes.forget", "input": {"memory": "$found", "memory_id": "mem_1"}},
            "Give `memory` or `memory_id`, not both.",
        ),
        (
            {"op": "notes.confirm", "input": {}},
            "Name the note: `memory` for one an earlier step found, or `memory_id`.",
        ),
    ],
)
async def test_a_note_named_in_a_way_the_operation_cannot_use_is_refused_with_the_fix(
    step: dict[str, object], said: str
) -> None:
    result, _http = await _held(plan=_found_and(step))
    act = result["steps"][1]
    assert act["status"] == "error"
    assert str(act["error"]).endswith(said)


async def test_a_reference_to_a_search_that_found_nothing_is_refused_with_the_fix() -> None:
    http = FakeHttp(Answer(body={"data": []}), Answer(body={"data": []}))
    capabilities, context = _capabilities(http, permission_mode="auto")
    context.grants["notes.erase"] = Grant("notes.erase", "allow", "*")
    await capabilities.probe(context)
    result = await capabilities.execute(
        _found_and({"op": "notes.forget", "input": {"memory": "$found"}}), context
    )
    assert str(result["steps"][1]["error"]).endswith(
        "The referenced step found no note; find it first."
    )
