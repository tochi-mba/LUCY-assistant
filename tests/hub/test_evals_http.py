"""`HttpHub`: the harness's routes, on the same client, URL and token as every `lucy` command.

Two halves. The first pins the translation at the edge -- what each unusual answer becomes
-- against a hand-written transport. The second runs the adapter against the real hub,
in-process, because the harness exists to catch what fakes miss: if a route, a field name
or the session-grant spelling drifts, this is where it shows.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import httpx
import pytest
from conftest import ACCOUNT, AUDIENCE
from keyring_client.testing import ISSUER, mint

from lucy_api.cli.evals_hub import HttpHub
from lucy_api.evals.direct import invoke
from lucy_api.evals.hub import HubError, HubUnreachable
from lucy_api.evals.scenario import Invocation

if TYPE_CHECKING:
    from collections.abc import Callable

    from httpx import AsyncClient

URL = "http://127.0.0.1:8000"


def hub(answer: Callable[[httpx.Request], httpx.Response]) -> tuple[HttpHub, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpHub(client, URL + "/", "secret-token"), seen


# --------------------------------------------------------------------------------------
# The edge
# --------------------------------------------------------------------------------------


def test_every_request_carries_the_token_and_only_writes_carry_an_idempotency_key() -> None:
    adapter, seen = hub(lambda _: httpx.Response(200, json={"id": "x", "data": []}))
    adapter.create_session({"model": "clyde:haiku"})
    adapter.send_message("ses/1", "Hello")
    adapter.answer_approval("ses/1", "apr_1", approved=False, lifetime="session")
    adapter.turn("trn/1")
    adapter.items("ses_1", None)
    adapter.items("ses_1", "itm_9")
    adapter.revoke("workspace.change", "session:ses_1")

    assert all(request.headers["authorization"] == "Bearer secret-token" for request in seen)
    keyed = [request.url.raw_path for request in seen if "idempotency-key" in request.headers]
    assert keyed == [
        b"/v1/sessions",
        b"/v1/sessions/ses%2F1/inputs",
        b"/v1/sessions/ses%2F1/inputs",
    ]
    assert json.loads(seen[2].content) == {
        "events": [
            {
                "type": "input.approval",
                "approval_id": "apr_1",
                "approved": False,
                "lifetime": "session",
            }
        ]
    }
    assert seen[3].url.raw_path == b"/v1/turns/trn%2F1"
    assert dict(seen[4].url.params) == {"limit": "100", "order": "asc"}
    assert dict(seen[5].url.params) == {"limit": "100", "order": "asc", "after": "itm_9"}
    assert seen[6].method == "DELETE"
    assert dict(seen[6].url.params) == {"profile": "session:ses_1"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(409, json={"detail": "Answer one approval at a time."}),
            "POST /v1/turns/t/cancel answered 409: Answer one approval at a time.",
        ),
        (
            httpx.Response(502, text="<html>bad gateway</html>"),
            "POST /v1/turns/t/cancel answered 502: Bad Gateway",
        ),
        (
            httpx.Response(404, json={"detail": ""}),
            "POST /v1/turns/t/cancel answered 404: Not Found",
        ),
        (httpx.Response(499, json=[1]), "POST /v1/turns/t/cancel answered 499: no detail"),
    ],
)
def test_a_refusal_carries_the_hub_s_own_sentence(response: httpx.Response, message: str) -> None:
    adapter, _ = hub(lambda _: response)
    with pytest.raises(HubError) as caught:
        adapter.cancel_turn("t")
    assert str(caught.value) == message
    assert caught.value.status == response.status_code


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(200, text="not json"),
            "GET /healthy answered with something that is not JSON",
        ),
        (
            httpx.Response(200, json=["a", "list"]),
            "GET /healthy answered with JSON that is not an object",
        ),
    ],
)
def test_an_answer_that_is_not_an_object_is_refused(response: httpx.Response, message: str) -> None:
    adapter, _ = hub(lambda _: response)
    with pytest.raises(HubError, match=message) as caught:
        adapter.health()
    assert caught.value.fatal is False


def test_a_listing_without_rows_is_refused_and_rows_that_are_not_objects_are_dropped() -> None:
    adapter, _ = hub(lambda _: httpx.Response(200, json={"data": "nope"}))
    with pytest.raises(HubError, match="GET /v1/capabilities answered without a data list"):
        adapter.capabilities("personal")
    rows, _ = hub(lambda _: httpx.Response(200, json={"data": [{"id": "notes"}, "junk"]}))
    assert rows.permissions("personal") == [{"id": "notes"}]


def test_no_answer_at_all_is_unreachable_and_fatal() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter, _ = hub(refuse)
    with pytest.raises(HubUnreachable) as caught:
        adapter.models()
    assert str(caught.value) == f"cannot reach Lucy at {URL}"
    assert caught.value.fatal is True
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


@pytest.mark.parametrize(("status", "fatal"), [(401, True), (403, True), (404, False), (0, False)])
def test_only_a_refused_identity_is_fatal(status: int, fatal: bool) -> None:
    assert HubError("x", status=status).fatal is fatal


# --------------------------------------------------------------------------------------
# The real hub, in-process
# --------------------------------------------------------------------------------------


class Bridge(httpx.BaseTransport):
    """A synchronous transport onto the hub's app, which runs on the test's event loop.

    `HttpHub` is synchronous because `lucy` is; the app under test is asynchronous. The
    harness code runs in a worker thread and every request it makes is handed back to the
    loop, so the adapter meets the real routes, the real gate and the real store.
    """

    def __init__(self, client: AsyncClient, loop: asyncio.AbstractEventLoop) -> None:
        self._client = client
        self._loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def forward() -> httpx.Response:
            answer = await self._client.request(
                request.method,
                request.url.raw_path.decode(),
                headers={
                    name: value
                    for name, value in request.headers.items()
                    if name in {"authorization", "idempotency-key", "content-type"}
                },
                content=request.content,
            )
            return httpx.Response(
                answer.status_code, json=answer.json() if answer.content else None
            )

        return asyncio.run_coroutine_threadsafe(forward(), self._loop).result(timeout=30)


async def against_the_hub(client: AsyncClient, conversation: Callable[[HttpHub], None]) -> None:
    """Run synchronous harness code against the in-process hub."""
    loop = asyncio.get_running_loop()
    token = mint(account_id=ACCOUNT, audience=AUDIENCE, issuer=ISSUER)

    def work() -> None:
        with httpx.Client(transport=Bridge(client, loop)) as sync:
            conversation(HttpHub(sync, "http://test", token))

    await asyncio.to_thread(work)


async def test_the_routes_the_harness_reads_exist_and_answer_in_the_shape_it_reads(
    workspace_client: AsyncClient,
) -> None:
    def conversation(real: HttpHub) -> None:
        assert isinstance(real.health()["version"], str)
        assert {"ready", "available", "unavailable"} <= set(real.models())
        capabilities = real.capabilities("personal")
        assert capabilities
        assert all({"id", "usable", "state", "detail"} <= set(row) for row in capabilities)
        permissions = real.permissions("personal")
        assert any("workspace.write" in row["covers"] for row in permissions)

        session = real.create_session(
            {
                "title": "[eval] contract",
                "model": "scripted:demo",
                "profile": "personal",
                "permission_mode": "ask",
                "incognito": False,
                "input_policy": "enqueue",
            }
        )
        assert session["title"] == "[eval] contract"
        assert session["input_policy"] == "enqueue"
        listing = real.tools(session["id"])
        assert isinstance(listing["tools"], list)
        assert isinstance(listing["deferred"], list)
        assert "turn_cache_read_tokens" in real.usage(session["id"])
        page = real.items(session["id"], None)
        assert page["data"] == []
        assert page["has_more"] is False
        real.archive(session["id"])

    await against_the_hub(workspace_client, conversation)


async def test_a_seed_write_is_granted_for_its_session_only_and_the_grant_is_gone_after(
    workspace_client: AsyncClient,
) -> None:
    def conversation(real: HttpHub) -> None:
        session = real.create_session({"model": "scripted:demo", "permission_mode": "ask"})
        scope = {"session_id": session["id"], "profile": session["profile"]}
        planted = Invocation(op="workspace.write", input={"path": "notes.md", "content": "Hi"})

        written = invoke(real, planted, **scope)

        assert written.status == "ok", written.error
        read = invoke(real, Invocation(op="workspace.read", input={"path": "notes.md"}), **scope)
        assert read.status == "ok", read.error
        assert "Hi" in read.output
        mine = [
            row["grant"] for row in real.permissions("personal") if row["id"] == "workspace.change"
        ]
        assert mine == [None], "the person's own grant was never touched"
        with pytest.raises(HubError) as refused:
            real.invoke("workspace.write", {"path": "x.md", "content": "y"}, session["id"])
        assert refused.value.status == 409, "the session grant was taken back"

    await against_the_hub(workspace_client, conversation)
