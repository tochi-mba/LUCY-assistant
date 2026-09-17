"""The session surface, driven in-process against a fake keyring.

What is being checked is mostly not "does the route work". It is the three properties the
route has to have and that nothing else in the system can enforce for it.

**The account comes from the token.** Every test that reaches for somebody else's session
does it the only way a caller could -- by knowing the id -- and gets the same 404 a made-up
id gets. There is no header, parameter or body field to try, and the contract test proves
that structurally; these prove it end to end.

**A retry is not a second request.** Creating a session is the one write here that must
survive a dropped connection, so the key is required, a replay returns the original, and the
same key over a different body is a 409 rather than a quiet second session.

**Paging is cursor-only and strict.** A misspelled query parameter is refused rather than
ignored, because a filter that silently did not apply reads to the caller exactly like a
filter that did.

A real database and the real app: the seam is `transport=`, which is keyring, and nothing
below the route is faked. Half of what these assert is transactional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient

from lucy_api.api.app import create_app
from lucy_api.api.dependencies import StreamCursor
from lucy_api.api.routers.sessions import stream_session_events
from lucy_api.api.schemas.problem import PROBLEM_CONTENT_TYPE
from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import Outcome
from lucy_api.sessions.sql_store import NewItem
from lucy_api.sessions.turns import close_turn, open_turn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container
    from lucy_api.sessions.sql_store import SessionStore

OTHER = "acct_someone_else"


@dataclass(frozen=True, slots=True)
class Hub:
    """The app over HTTP, plus the store behind it.

    The store is here because the one write path that creates a turn belongs to the turn
    loop and is not part of this surface yet. Reaching past HTTP to set a turn up is honest
    about that: these tests are about reading and cancelling turns, not about starting them.
    """

    http: AsyncClient
    store: SessionStore
    container: Container


@pytest.fixture
async def hub(settings: Settings, keyring: FakeKeyring) -> AsyncIterator[Hub]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        yield Hub(http=http, store=container.store, container=container)


async def create(hub: Hub, key: str = "key-1", **body: Any) -> dict[str, Any]:
    response = await hub.http.post(
        "/v1/sessions", json=body, headers={**bearer(), "Idempotency-Key": key}
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def a_turn(hub: Hub, session: str, status: str = "running") -> str:
    turn = await open_turn(hub.store, ACCOUNT, session, {"events": []})
    await close_turn(hub.store, ACCOUNT, str(turn["id"]), Outcome(status))
    return str(turn["id"])


class TestCreating:
    async def test_a_new_session_is_201_and_starts_idle(self, hub: Hub) -> None:
        created = await create(hub, title="Planning")

        assert created["title"] == "Planning"
        assert created["status"] == "idle"
        assert created["input_policy"] == "enqueue"
        assert created["id"].startswith("ses_")

    async def test_a_session_document_never_names_the_account_that_owns_it(self, hub: Hub) -> None:
        # Not squeamishness: a session document gets pasted into bug reports.
        response = await hub.http.post(
            "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "key-1"}
        )

        assert ACCOUNT not in response.text
        assert "account_id" not in response.json()

    async def test_creating_without_an_idempotency_key_is_refused(self, hub: Hub) -> None:
        response = await hub.http.post("/v1/sessions", json={}, headers=bearer())

        assert response.status_code == 422
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert any("Idempotency-Key" in error["location"] for error in response.json()["errors"])

    async def test_retrying_with_the_same_key_returns_the_first_session(self, hub: Hub) -> None:
        first = await create(hub, title="Planning")
        again = await create(hub, key="key-1", title="Planning")

        assert again["id"] == first["id"]
        listed = await hub.http.get("/v1/sessions", headers=bearer())
        assert len(listed.json()["data"]) == 1

    async def test_one_key_used_for_a_different_request_is_a_conflict(self, hub: Hub) -> None:
        await create(hub, title="Planning")

        response = await hub.http.post(
            "/v1/sessions",
            json={"title": "Something else"},
            headers={**bearer(), "Idempotency-Key": "key-1"},
        )

        assert response.status_code == 409
        assert response.json()["type"].endswith("/conflict")

    async def test_an_unknown_field_in_the_body_is_refused(self, hub: Hub) -> None:
        response = await hub.http.post(
            "/v1/sessions",
            json={"account_id": OTHER},
            headers={**bearer(), "Idempotency-Key": "key-1"},
        )

        # There is nowhere to put somebody else's id, and pretending to accept one would
        # read to the caller exactly like it worked.
        assert response.status_code == 422

    async def test_creating_without_a_token_is_401(self, hub: Hub) -> None:
        response = await hub.http.post(
            "/v1/sessions", json={}, headers={"Idempotency-Key": "key-1"}
        )

        assert response.status_code == 401


class TestReading:
    async def test_a_session_reads_back_by_id(self, hub: Hub) -> None:
        created = await create(hub)

        response = await hub.http.get(f"/v1/sessions/{created['id']}", headers=bearer())

        assert response.status_code == 200
        assert response.json()["id"] == created["id"]

    async def test_another_accounts_session_answers_exactly_as_a_missing_one(
        self, hub: Hub
    ) -> None:
        created = await create(hub)

        foreign = await hub.http.get(
            f"/v1/sessions/{created['id']}", headers=bearer(account_id=OTHER)
        )
        invented = await hub.http.get("/v1/sessions/ses_invented", headers=bearer())

        assert foreign.status_code == invented.status_code == 404
        assert foreign.json()["detail"] == invented.json()["detail"]

    async def test_a_listing_pages_by_cursor_and_says_whether_there_is_more(self, hub: Hub) -> None:
        made = [(await create(hub, key=f"key-{index}"))["id"] for index in range(3)]

        first = await hub.http.get("/v1/sessions?limit=2", headers=bearer())
        rest = await hub.http.get(
            f"/v1/sessions?limit=2&after={first.json()['last_id']}", headers=bearer()
        )

        assert [row["id"] for row in first.json()["data"]] == made[:2]
        assert first.json() == {
            "data": first.json()["data"],
            "has_more": True,
            "first_id": made[0],
            "last_id": made[1],
        }
        assert [row["id"] for row in rest.json()["data"]] == made[2:]
        assert rest.json()["has_more"] is False

    async def test_a_listing_holds_only_this_accounts_sessions(self, hub: Hub) -> None:
        await create(hub)

        response = await hub.http.get("/v1/sessions", headers=bearer(account_id=OTHER))

        assert response.json() == {
            "data": [],
            "has_more": False,
            "first_id": None,
            "last_id": None,
        }

    async def test_a_cursor_that_is_not_in_the_collection_is_refused(self, hub: Hub) -> None:
        # A stale bookmark and an exhausted collection are different answers, and only one
        # of them means reload.
        await create(hub)

        response = await hub.http.get("/v1/sessions?after=ses_invented", headers=bearer())

        assert response.status_code == 404

    @pytest.mark.parametrize(
        "query", ["limit=0", "limit=101", "order=sideways", "ordr=desc", "limit=lots"]
    )
    async def test_a_selection_that_does_not_make_sense_is_refused(
        self, hub: Hub, query: str
    ) -> None:
        response = await hub.http.get(f"/v1/sessions?{query}", headers=bearer())

        assert response.status_code == 422
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)


class TestUpdating:
    async def test_only_the_fields_that_were_sent_are_changed(self, hub: Hub) -> None:
        created = await create(hub, title="Planning")

        response = await hub.http.patch(
            f"/v1/sessions/{created['id']}",
            json={"permission_mode": "plan"},
            headers=bearer(),
        )

        assert response.json()["permission_mode"] == "plan"
        assert response.json()["title"] == "Planning"

    async def test_archiving_is_reversible(self, hub: Hub) -> None:
        created = await create(hub)

        archived = await hub.http.patch(
            f"/v1/sessions/{created['id']}", json={"archived": True}, headers=bearer()
        )
        restored = await hub.http.patch(
            f"/v1/sessions/{created['id']}", json={"archived": False}, headers=bearer()
        )

        assert archived.json()["archived_at"] is not None
        assert restored.json()["archived_at"] is None

    async def test_the_model_cannot_be_changed_halfway_through_a_conversation(
        self, hub: Hub
    ) -> None:
        # A transcript that records two different assistants is not a transcript.
        created = await create(hub)

        response = await hub.http.patch(
            f"/v1/sessions/{created['id']}", json={"model": "openai:gpt-4"}, headers=bearer()
        )

        assert response.status_code == 422

    async def test_updating_another_accounts_session_finds_nothing(self, hub: Hub) -> None:
        created = await create(hub)

        response = await hub.http.patch(
            f"/v1/sessions/{created['id']}",
            json={"title": "taken over"},
            headers=bearer(account_id=OTHER),
        )

        assert response.status_code == 404
        current = await hub.http.get(f"/v1/sessions/{created['id']}", headers=bearer())
        assert current.json()["title"] == "New conversation"


class TestDeleting:
    async def test_a_deleted_session_answers_with_nothing_and_is_then_gone(self, hub: Hub) -> None:
        created = await create(hub)

        response = await hub.http.delete(f"/v1/sessions/{created['id']}", headers=bearer())

        assert response.status_code == 204
        assert response.content == b""
        after = await hub.http.get(f"/v1/sessions/{created['id']}", headers=bearer())
        assert after.status_code == 404

    async def test_deleting_another_accounts_session_finds_nothing_and_changes_nothing(
        self, hub: Hub
    ) -> None:
        created = await create(hub)

        response = await hub.http.delete(
            f"/v1/sessions/{created['id']}", headers=bearer(account_id=OTHER)
        )

        assert response.status_code == 404
        still = await hub.http.get(f"/v1/sessions/{created['id']}", headers=bearer())
        assert still.status_code == 200


class TestForking:
    async def test_a_fork_is_a_new_session_holding_a_copy_of_the_transcript(self, hub: Hub) -> None:
        created = await create(hub)
        await hub.store.append(
            ACCOUNT, created["id"], NewItem("message", "user", {"text": "hello"})
        )

        response = await hub.http.post(
            f"/v1/sessions/{created['id']}/fork", json={}, headers=bearer()
        )

        assert response.status_code == 201
        fork = response.json()
        assert fork["id"] != created["id"]
        assert fork["parent_session_id"] == created["id"]
        copies = await hub.http.get(f"/v1/sessions/{fork['id']}/items", headers=bearer())
        assert [row["content"] for row in copies.json()["data"]] == [{"text": "hello"}]

    async def test_a_fork_can_be_taken_at_a_point_in_the_transcript(self, hub: Hub) -> None:
        created = await create(hub)
        first = await hub.store.append(
            ACCOUNT, created["id"], NewItem("message", "user", {"text": "one"})
        )
        await hub.store.append(ACCOUNT, created["id"], NewItem("message", "user", {"text": "two"}))

        response = await hub.http.post(
            f"/v1/sessions/{created['id']}/fork",
            json={"item_id": first["id"]},
            headers=bearer(),
        )

        copies = await hub.http.get(f"/v1/sessions/{response.json()['id']}/items", headers=bearer())
        assert [row["content"] for row in copies.json()["data"]] == [{"text": "one"}]

    async def test_forking_at_an_item_from_somewhere_else_is_refused(self, hub: Hub) -> None:
        created = await create(hub)
        elsewhere = await create(hub, key="key-2")
        foreign = await hub.store.append(
            ACCOUNT, elsewhere["id"], NewItem("message", "user", {"text": "over there"})
        )

        response = await hub.http.post(
            f"/v1/sessions/{created['id']}/fork",
            json={"item_id": foreign["id"]},
            headers=bearer(),
        )

        assert response.status_code == 404

    async def test_forking_another_accounts_session_finds_nothing(self, hub: Hub) -> None:
        created = await create(hub)

        response = await hub.http.post(
            f"/v1/sessions/{created['id']}/fork", json={}, headers=bearer(account_id=OTHER)
        )

        assert response.status_code == 404


class TestItems:
    async def test_a_transcript_pages_oldest_first(self, hub: Hub) -> None:
        created = await create(hub)
        written = [
            await hub.store.append(
                ACCOUNT, created["id"], NewItem("message", "user", {"text": str(index)})
            )
            for index in range(3)
        ]

        response = await hub.http.get(
            f"/v1/sessions/{created['id']}/items?limit=2", headers=bearer()
        )

        body = response.json()
        assert [row["id"] for row in body["data"]] == [row["id"] for row in written[:2]]
        assert body["has_more"] is True

    async def test_one_item_reads_back_by_its_own_id(self, hub: Hub) -> None:
        created = await create(hub)
        written = await hub.store.append(
            ACCOUNT, created["id"], NewItem("message", "user", {"text": "hello"})
        )

        response = await hub.http.get(f"/v1/items/{written['id']}", headers=bearer())

        assert response.status_code == 200
        assert response.json()["content"] == {"text": "hello"}
        assert response.json()["seq"] == 1

    async def test_another_accounts_item_finds_nothing(self, hub: Hub) -> None:
        created = await create(hub)
        written = await hub.store.append(
            ACCOUNT, created["id"], NewItem("message", "user", {"text": "private"})
        )

        response = await hub.http.get(
            f"/v1/items/{written['id']}", headers=bearer(account_id=OTHER)
        )

        assert response.status_code == 404

    async def test_the_transcript_of_another_accounts_session_finds_nothing(self, hub: Hub) -> None:
        created = await create(hub)

        response = await hub.http.get(
            f"/v1/sessions/{created['id']}/items", headers=bearer(account_id=OTHER)
        )

        assert response.status_code == 404


class TestTurns:
    async def test_a_message_enters_through_the_one_write_path_and_queues_a_turn(
        self, hub: Hub
    ) -> None:
        created = await create(hub)

        response = await hub.http.post(
            f"/v1/sessions/{created['id']}/inputs",
            json={"events": [{"type": "input.message", "content": "Find a good album."}]},
            headers={**bearer(), "Idempotency-Key": "turn-key"},
        )

        assert response.status_code == 202
        turn = response.json()
        assert turn["status"] == "queued"
        assert response.headers["location"] == f"/v1/turns/{turn['id']}"
        transcript = await hub.http.get(f"/v1/sessions/{created['id']}/items", headers=bearer())
        assert transcript.json()["data"][0]["content"] == "Find a good album."
        assert transcript.json()["data"][0]["turn_id"] == turn["id"]

    async def test_replaying_an_input_key_returns_the_original_turn_without_a_second_message(
        self, hub: Hub
    ) -> None:
        created = await create(hub)
        body = {"events": [{"type": "input.message", "content": "Hello."}]}
        headers = {**bearer(), "Idempotency-Key": "turn-key"}

        first = await hub.http.post(
            f"/v1/sessions/{created['id']}/inputs", json=body, headers=headers
        )
        again = await hub.http.post(
            f"/v1/sessions/{created['id']}/inputs", json=body, headers=headers
        )

        assert first.status_code == again.status_code == 202
        assert first.json()["id"] == again.json()["id"]
        items = await hub.http.get(f"/v1/sessions/{created['id']}/items", headers=bearer())
        assert len(items.json()["data"]) == 1

    async def test_a_sessions_turns_are_listed_in_the_order_they_were_opened(
        self, hub: Hub
    ) -> None:
        created = await create(hub)
        first = await a_turn(hub, created["id"])
        second = await a_turn(hub, created["id"], status="queued")

        response = await hub.http.get(f"/v1/sessions/{created['id']}/turns", headers=bearer())

        assert [row["id"] for row in response.json()["data"]] == [first, second]

    async def test_one_turn_reads_back_with_how_it_ended(self, hub: Hub) -> None:
        created = await create(hub)
        turn = await a_turn(hub, created["id"])
        await close_turn(
            hub.store, ACCOUNT, turn, Outcome("failed", "error_max_iterations", "end_turn")
        )

        response = await hub.http.get(f"/v1/turns/{turn}", headers=bearer())

        body = response.json()
        assert body["status"] == "failed"
        assert body["termination"] == "error_max_iterations"
        assert body["stop_reason"] == "end_turn"

    async def test_another_accounts_turn_finds_nothing(self, hub: Hub) -> None:
        created = await create(hub)
        turn = await a_turn(hub, created["id"])

        response = await hub.http.get(f"/v1/turns/{turn}", headers=bearer(account_id=OTHER))

        assert response.status_code == 404

    async def test_cancelling_a_running_turn_records_the_request(self, hub: Hub) -> None:
        created = await create(hub)
        turn = await a_turn(hub, created["id"])

        response = await hub.http.post(f"/v1/turns/{turn}/cancel", headers=bearer())

        assert response.status_code == 200
        assert response.json()["cancel_requested"] is True
        assert response.json()["status"] == "running"

    async def test_cancelling_twice_answers_the_same_thing_twice(self, hub: Hub) -> None:
        created = await create(hub)
        turn = await a_turn(hub, created["id"], status="queued")

        once = await hub.http.post(f"/v1/turns/{turn}/cancel", headers=bearer())
        twice = await hub.http.post(f"/v1/turns/{turn}/cancel", headers=bearer())

        assert once.json() == twice.json()
        assert once.json()["status"] == "cancelled"

    async def test_cancelling_another_accounts_turn_finds_nothing(self, hub: Hub) -> None:
        created = await create(hub)
        turn = await a_turn(hub, created["id"])

        response = await hub.http.post(f"/v1/turns/{turn}/cancel", headers=bearer(account_id=OTHER))

        assert response.status_code == 404
        assert (await hub.store.turn(ACCOUNT, turn))["cancel_requested"] == 0


class TestEventStream:
    async def test_opening_a_stream_sends_a_snapshot_then_the_durable_log(self, hub: Hub) -> None:
        created = await create(hub, title="Follow this")
        response = await stream_session_events(
            created["id"],
            VerifiedCaller(ACCOUNT, "lucy-api"),
            hub.store,
            hub.container,
            StreamCursor(starting_after="0", last_event_id=None),
        )

        body = aiter(response.body_iterator)
        first = await anext(body)
        snapshot = await anext(body)
        resumed = await anext(body)
        created_event = await anext(body)

        assert first.startswith("retry:")
        assert "lucy.stream.snapshot" in snapshot
        assert "lucy.stream.resumed" in resumed
        assert "lucy.session.created" in created_event
        assert '"title":"Follow this"' in snapshot

    async def test_a_committed_input_reaches_an_already_open_stream(self, hub: Hub) -> None:
        created = await create(hub)
        response = await stream_session_events(
            created["id"],
            VerifiedCaller(ACCOUNT, "lucy-api"),
            hub.store,
            hub.container,
            StreamCursor(starting_after=None, last_event_id=None),
        )
        body = aiter(response.body_iterator)
        await anext(body)  # Opening frame.
        await anext(body)  # Snapshot; subscription is now registered.

        submitted = await hub.http.post(
            f"/v1/sessions/{created['id']}/inputs",
            headers={**bearer(), "Idempotency-Key": "stream-input"},
            json={"events": [{"type": "input.message", "content": "Hello"}]},
        )
        item = await anext(body)
        turn = await anext(body)

        assert submitted.status_code == 202
        assert "lucy.content.item.added" in item
        assert "lucy.turn.created" in turn

    async def test_a_stream_checks_ownership_before_it_subscribes(self, hub: Hub) -> None:
        created = await create(hub)
        with pytest.raises(LucyError) as caught:
            await stream_session_events(
                created["id"],
                VerifiedCaller(OTHER, "lucy-api"),
                hub.store,
                hub.container,
                StreamCursor(starting_after=None, last_event_id=None),
            )

        assert caught.value.status == 404
        assert hub.container.events.subscriber_count(created["id"]) == 0

    async def test_the_ui_encoding_is_chosen_when_both_headers_arrive(self, hub: Hub) -> None:
        """A browser client asking for the AI-SDK encoding is answered in that encoding.

        Called directly rather than over the test client, like its three neighbours above.
        An event stream does not end -- that is what makes it a stream -- and the in-process
        ASGI transport runs an application to completion before it answers, so a request for
        one through `hub.http` never returns. A test that does that hangs the suite instead
        of failing it, which is far worse than a red test: nobody can tell it apart from a
        slow machine.
        """
        created = await create(hub)
        response = await stream_session_events(
            created["id"],
            VerifiedCaller(ACCOUNT, "lucy-api"),
            hub.store,
            hub.container,
            StreamCursor(starting_after=None, last_event_id=None),
            accept="text/event-stream",
            ui_stream="v1",
        )

        assert response.status_code == 200
        assert response.headers["x-vercel-ai-ui-message-stream"] == "v1"
        assert response.headers["x-lucy-ui-message-stream"] == "v1"

        # The generator holds a subscription open until something drains it. Closing it is
        # what a disconnecting client does, and leaving it open leaks a subscriber into
        # every test that runs after this one.
        await response.body_iterator.aclose()
