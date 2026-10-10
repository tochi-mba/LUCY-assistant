"""A person can see how full the window is, and compact whenever they like.

Automatic compaction keeps running on its own. These tests pin the other half: the figure a
person is shown is the one the hub acts on, a compaction they ask for says what it did to
that figure, every row says who asked for it, and a breaker that switched the automatic one
off does not stop a person from trying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from asgi_lifespan import LifespanManager
from conftest import ACCOUNT, bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.core.errors import LucyError
from lucy_api.sessions.compact import (
    FAILURES_BEFORE_DISABLE,
    MAX_KEEP_RECENT_TURNS,
    compact_session,
    list_compactions,
    uncompact_session,
)
from lucy_api.sessions.models import CreateSession, Outcome
from lucy_api.sessions.sql_store import NewItem
from lucy_api.sessions.turns import close_turn, open_turn
from lucy_api.turn.prompt import SessionView, window_report

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container
    from lucy_api.sessions.sql_store import SessionStore


@pytest.fixture
async def hub(
    settings: Settings, keyring: FakeKeyring
) -> AsyncIterator[tuple[AsyncClient, Container]]:
    app = create_app(settings, transport=keyring.transport())
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        container = app.state.container
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient()
        container.environment_override = FakeEnvironmentsClient()
        yield http, container


async def a_session(store: SessionStore) -> str:
    created = await store.create(ACCOUNT, CreateSession(model="scripted:demo"), "in-hand")
    return str(created["id"])


async def turns(store: SessionStore, session: str, count: int) -> None:
    for index in range(count):
        turn = await open_turn(store, ACCOUNT, session, {"n": index})
        tid = str(turn["id"])
        await store.append(
            ACCOUNT,
            session,
            NewItem("message", "user", f"question {index} " + ("word " * 300), turn=tid),
        )
        await store.append(
            ACCOUNT, session, NewItem("message", "assistant", "answer " * 300, turn=tid)
        )
        await close_turn(store, ACCOUNT, tid, Outcome("completed"))


def boom(*_args: object) -> str:
    raise LucyError("compact-failed", "the summariser could not run", 500)


# --------------------------------------------------------------------------------------
# The store: who asked, and the breaker that only stops the automatic one
# --------------------------------------------------------------------------------------


async def test_every_compaction_says_who_asked_for_it_and_how_full_the_window_was(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await turns(sessions_store, session, 3)
    automatic = await compact_session(
        sessions_store, ACCOUNT, session, trigger="auto", trigger_tokens=9_000
    )
    await turns(sessions_store, session, 1)
    manual = await compact_session(sessions_store, ACCOUNT, session, trigger_tokens=-5)

    assert (automatic["trigger"], automatic["trigger_tokens"]) == ("auto", 9_000)
    assert (manual["trigger"], manual["trigger_tokens"]) == ("manual", 0), "never negative"
    assert automatic["turns"] == 1
    assert manual["turns"] == 2
    events = await sessions_store.records(ACCOUNT, session, "events")
    applied = [event["data"] for event in events if event["type"] == "lucy.compaction.applied"]
    assert [entry["trigger"] for entry in applied] == ["auto", "manual"]


async def test_a_person_can_compact_after_the_automatic_one_switched_itself_off(
    sessions_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await a_session(sessions_store)
    await turns(sessions_store, session, 3)
    monkeypatch.setattr("lucy_api.sessions.compact._summary", boom)
    for _ in range(FAILURES_BEFORE_DISABLE):
        with pytest.raises(LucyError):
            await compact_session(sessions_store, ACCOUNT, session, trigger="auto")
    assert (await list_compactions(sessions_store, ACCOUNT, session))["automatic"] is False

    with pytest.raises(LucyError) as tried:
        await compact_session(sessions_store, ACCOUNT, session)
    assert tried.value.code == "compact-failed", "a person's ask is tried, not refused"

    monkeypatch.undo()
    written = await compact_session(sessions_store, ACCOUNT, session)
    assert written["active"] is True
    listed = await list_compactions(sessions_store, ACCOUNT, session)
    assert listed["automatic"] is True, "a compaction that works switches automatic back on"
    assert [row["id"] for row in listed["data"]] == [written["id"]], "the breaker is not a row"


async def test_the_listing_says_which_compaction_the_model_is_reading(
    sessions_store: SessionStore,
) -> None:
    session = await a_session(sessions_store)
    await turns(sessions_store, session, 3)
    older = await compact_session(sessions_store, ACCOUNT, session)
    await turns(sessions_store, session, 1)
    newer = await compact_session(sessions_store, ACCOUNT, session, trigger="auto")

    listed = (await list_compactions(sessions_store, ACCOUNT, session))["data"]
    assert [(row["id"], row["active"], row["shown"]) for row in listed] == [
        (newer["id"], True, True),
        (older["id"], True, False),
    ]
    assert listed[0]["trigger"] == "auto"
    assert listed[0]["summary"].startswith("The person asked")

    await uncompact_session(sessions_store, ACCOUNT, session, str(newer["id"]))
    listed = (await list_compactions(sessions_store, ACCOUNT, session))["data"]
    assert [(row["active"], row["shown"]) for row in listed] == [(False, False), (True, True)]


async def test_the_breaker_row_cannot_be_undone(sessions_store: SessionStore, monkeypatch) -> None:
    session = await a_session(sessions_store)
    await turns(sessions_store, session, 3)
    monkeypatch.setattr("lucy_api.sessions.compact._summary", boom)
    for _ in range(FAILURES_BEFORE_DISABLE):
        with pytest.raises(LucyError):
            await compact_session(sessions_store, ACCOUNT, session, trigger="auto")
    [marker] = await sessions_store.records(ACCOUNT, session, "compactions")

    with pytest.raises(LucyError) as caught:
        await uncompact_session(sessions_store, ACCOUNT, session, str(marker["id"]))
    assert caught.value.code == "not-found"


# --------------------------------------------------------------------------------------
# The gauge
# --------------------------------------------------------------------------------------


def a_view(words: int, *, window: int) -> SessionView:
    return SessionView(
        session_id="ses",
        items=[
            {
                "id": "itm_1",
                "seq": 1,
                "role": "user",
                "content": "word " * words,
                "turn_id": "trn_1",
                "type": "message",
            }
        ],
        window=window,
        warn_at_percent=60,
        compact_at_percent=72,
    )


@pytest.mark.parametrize(
    ("share", "state"),
    [(10, "ok"), (65, "warning"), (80, "compacting"), (200, "over")],
)
def test_the_gauge_names_the_state_the_window_is_in(share: int, state: str) -> None:
    """`share` is the percentage of the window the same request fills."""
    used = window_report(a_view(10, window=200_000))["used_tokens"]
    report = window_report(a_view(10, window=used * 100 // share))
    assert report["state"] == state, report


def test_the_gauge_says_how_far_compaction_is_and_never_below_zero() -> None:
    roomy = window_report(a_view(10, window=200_000))
    assert roomy["window_tokens"] == 200_000
    assert roomy["percent"] == int(100 * roomy["used_tokens"] / 200_000)
    assert roomy["tokens_until_compaction"] == 144_000 - roomy["used_tokens"]
    assert roomy["summarised_turns"] == 0
    assert roomy["warn_at_percent"] == 60
    assert roomy["compact_at_percent"] == 72
    assert window_report(a_view(10, window=1_000))["tokens_until_compaction"] == 0
    assert window_report(a_view(10, window=0))["percent"] == 0


# --------------------------------------------------------------------------------------
# Over HTTP
# --------------------------------------------------------------------------------------


async def new_session(http: AsyncClient, key: str) -> str:
    created = await http.post("/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": key})
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


async def test_compacting_by_hand_says_what_it_did_to_the_window(
    hub: tuple[AsyncClient, Container],
) -> None:
    http, container = hub
    session = await new_session(http, "in-hand-compact")
    await turns(container.store, session, 6)

    window = await http.get(f"/v1/sessions/{session}/context/window", headers=bearer())
    assert window.status_code == 200, window.text
    gauge = window.json()
    assert gauge["automatic_compaction"] is True
    assert gauge["compactions"] == 0
    assert gauge["used_tokens"] > 0

    done = await http.post(
        f"/v1/sessions/{session}/compact", json={"keep_recent_turns": 1}, headers=bearer()
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["trigger"] == "manual"
    assert body["keep_recent_turns"] == 1
    assert body["turns"] == 5
    assert body["context_before"]["used_tokens"] == gauge["used_tokens"]
    assert body["context_after"]["used_tokens"] < body["context_before"]["used_tokens"]
    assert body["context_after"]["summarised_turns"] == 5
    assert body["trigger_tokens"] == gauge["used_tokens"]

    again = await http.post(
        f"/v1/sessions/{session}/compact", json={"keep_recent_turns": 1}, headers=bearer()
    )
    assert again.status_code == 409
    assert "nothing new to compact" in again.text

    listed = await http.get(f"/v1/sessions/{session}/compactions", headers=bearer())
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [body["id"]]
    after = (await http.get(f"/v1/sessions/{session}/context/window", headers=bearer())).json()
    assert after["compactions"] == 1
    assert after["used_tokens"] == body["context_after"]["used_tokens"]

    context = await http.get(f"/v1/sessions/{session}/context", headers=bearer())
    assert context.json()["window"]["used_tokens"] == after["used_tokens"]


async def test_without_a_body_the_persons_setting_decides_what_stays(
    hub: tuple[AsyncClient, Container],
) -> None:
    http, container = hub
    session = await new_session(http, "in-hand-default")
    await turns(container.store, session, 6)
    done = await http.post(f"/v1/sessions/{session}/compact", headers=bearer())
    assert done.status_code == 200, done.text
    assert done.json()["keep_recent_turns"] == 4, "history_turns_kept's default"


@pytest.mark.parametrize(
    "body",
    [{"keep_recent_turns": -1}, {"keep_recent_turns": MAX_KEEP_RECENT_TURNS + 1}, {"keep": 1}],
)
async def test_a_bad_compact_body_is_refused_before_anything_is_written(
    hub: tuple[AsyncClient, Container], body: dict[str, int]
) -> None:
    http, container = hub
    session = await new_session(http, f"in-hand-bad-{sorted(body)}-{next(iter(body.values()))}")
    await turns(container.store, session, 3)
    refused = await http.post(f"/v1/sessions/{session}/compact", json=body, headers=bearer())
    assert refused.status_code == 422
    assert await container.store.records(ACCOUNT, session, "compactions") == []


async def test_compaction_still_happens_when_the_figures_cannot_be_read(
    hub: tuple[AsyncClient, Container], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compaction reads the transcript and nothing else; the gauge needs the turn machinery.
    When that cannot be built, the person's ask still lands and the figures are null."""
    http, container = hub
    session = await new_session(http, "in-hand-blind")
    await turns(container.store, session, 6)

    async def unavailable(*_args: object, **_kwargs: object) -> SessionView:
        raise LucyError("settings-unavailable", "settings could not be reached", 503)

    monkeypatch.setattr("lucy_api.api.routers.economy.session_view", unavailable)
    done = await http.post(f"/v1/sessions/{session}/compact", headers=bearer())

    assert done.status_code == 200, done.text
    assert done.json()["context_before"] is None
    assert done.json()["context_after"] is None
    assert done.json()["trigger_tokens"] == 0


async def test_another_accounts_session_has_no_window_or_compactions(
    hub: tuple[AsyncClient, Container],
) -> None:
    http, _container = hub
    for path in ("context/window", "compactions"):
        missing = await http.get(f"/v1/sessions/ses_nobody/{path}", headers=bearer())
        assert missing.status_code == 404, path
