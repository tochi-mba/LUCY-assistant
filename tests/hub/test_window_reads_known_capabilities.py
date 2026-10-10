"""The window gauge counts capabilities as the hub last found them, without asking again.

`GET /v1/sessions/{id}/context/window` took 6.1 s on its first read and 88 ms after
(measured on the live hub, 2026-10-10). Its figure includes the capability list and the plan
schema a turn would send, so it probed every capability, and once a turn's fifteen seconds had
passed every probe was a network round again: a keyring exchange per sibling, which keyring
answers one at a time, then each sibling's reply. Profiled in the hub's own container, the
cold read spent 4.8 of its 6.0 seconds in probes and 2.3 in the exchanges inside them.

A probe that found a credential missing also dropped every other capability's answer, so for a
person with one capability not connected nothing stayed cached past a probe round.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from asgi_lifespan import LifespanManager
from conftest import bearer
from httpx import ASGITransport, AsyncClient
from settings_client.testing import FakeSettingsClient
from test_packs_registry import Gadget

from lucy_api.api.app import create_app
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.packs.base import Availability, State
from lucy_api.packs.context import Call
from lucy_api.packs.help import HelpPack
from lucy_api.packs.probes import KNOWN_SECONDS, PROBE_TTL_SECONDS, GuardedHttp, ProbeCache
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from keyring_client.testing import FakeKeyring

    from lucy_api.core.config import Settings
    from lucy_api.core.container import Container

MISSING = {"type": "https://example.test/problems/credential-unavailable"}


class Counting(Gadget):
    """A ready capability that counts how often it was asked."""

    def __init__(self, pack_id: str = "gadget") -> None:
        super().__init__(pack_id)
        self.probes = 0

    async def probe(self, context: object) -> Availability:
        self.probes += 1
        return await super().probe(context)


class NotConnected(Counting):
    """A capability whose sibling answers its probe: this person's credential is missing."""

    async def probe(self, context: Any) -> Availability:
        self.probes += 1
        await context.http.request_response(
            Call(method="GET", url="https://repos.test/v1/me", audience="github-api")
        )
        return Availability(state=State.not_connected, detail="not connected")


class Answers:
    """A sibling that says the person's credential is missing, to every call."""

    async def request(self, call: Call) -> Any:
        del call
        return MISSING

    async def request_response(self, call: Call) -> Any:
        del call
        return SimpleNamespace(status_code=502, json=lambda: MISSING)


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


async def test_the_window_does_not_ask_every_capability_again_once_a_turn_would(
    hub: tuple[AsyncClient, Container],
) -> None:
    """The bug, named: the first read of the window took 6.1 s, because past a turn's fifteen
    seconds the gauge asked every sibling again -- a keyring exchange and a reply each -- for a
    token count that the answers it already had gave as well."""
    http, container = hub
    clock = [1_000.0]
    gadget = Counting()
    container.capabilities.packs = (HelpPack(), gadget)
    container.capabilities.probes = ProbeCache(now=lambda: clock[0])
    created = await http.post(
        "/v1/sessions", json={}, headers={**bearer(), "Idempotency-Key": "known-window"}
    )
    session = str(created.json()["id"])
    path = f"/v1/sessions/{session}/context/window"

    first = await http.get(path, headers=bearer())
    asked = gadget.probes
    clock[0] += PROBE_TTL_SECONDS * 4
    again = await http.get(path, headers=bearer())

    assert first.status_code == again.status_code == 200
    assert asked >= 1
    assert gadget.probes == asked, "a turn would ask again by now; the gauge need not"
    assert again.json() == first.json()

    clock[0] += KNOWN_SECONDS
    await http.get(path, headers=bearer())
    assert gadget.probes == asked + 1, "an answer an hour old is not taken"


async def test_a_turn_still_asks_again_once_its_fifteen_seconds_are_up() -> None:
    clock = [0.0]
    gadget = Counting()
    capabilities = Capabilities((gadget,), probes=ProbeCache(now=lambda: clock[0]))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    await capabilities.probe(context)
    clock[0] = PROBE_TTL_SECONDS

    await capabilities.probe(context, within=KNOWN_SECONDS)
    assert gadget.probes == 1
    await capabilities.probe(context)
    assert gadget.probes == 2


def test_an_answer_older_than_anybody_takes_is_forgotten() -> None:
    clock = [0.0]
    cache = ProbeCache(now=lambda: clock[0])
    ready = Availability(state=State.ready)
    cache.put("acct_a", "personal", "gadget", ready, session_id="ses_a")

    clock[0] = KNOWN_SECONDS - 1
    assert cache.get("acct_a", "personal", "gadget", session_id="ses_a") is None
    assert cache.get("acct_a", "personal", "gadget", session_id="ses_a", within=KNOWN_SECONDS)

    clock[0] = KNOWN_SECONDS
    assert (
        cache.get("acct_a", "personal", "gadget", session_id="ses_a", within=2 * KNOWN_SECONDS)
        is None
    )
    cache.put("acct_a", "personal", "other", ready)
    assert cache._rows.keys() == {("acct_a", "personal", "", "other")}


async def test_a_probe_that_finds_a_credential_missing_keeps_the_other_answers() -> None:
    """The bug, named: a probe whose sibling said the person's credential was missing dropped
    every cached answer the person had, so with one capability not connected nothing stayed
    cached past one probe round: every capability that had answered first was asked again."""
    ready = Counting("ready")
    missing = NotConnected("missing")
    capabilities = Capabilities((ready, missing))
    dropped: list[str] = []

    def forget() -> None:
        dropped.append("all")
        capabilities.forget_probes("acct_a", "personal")

    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a"),
        http=GuardedHttp(Answers(), account_id="acct_a", profile="personal", on_disconnect=forget),
    )

    first = await capabilities.probe(context)
    await capabilities.probe(context)

    assert dropped == []
    assert (ready.probes, missing.probes) == (1, 1)
    assert first.get("missing").availability.state is State.not_connected

    await context.http.request_response(
        Call(method="POST", url="https://repos.test/v1/issues", audience="github-api")
    )
    assert dropped == ["all"], "a step that finds it missing still drops them"
    await capabilities.probe(context)
    assert (ready.probes, missing.probes) == (2, 2)
