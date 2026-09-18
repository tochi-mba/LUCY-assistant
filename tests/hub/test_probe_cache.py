"""Probes are cached per person and pack, and a connect, disconnect, settings
write or missing credential must not leave a stale ready state in that cache."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from test_packs_registry import Gadget

from lucy_api.clients.errors import CREDENTIAL_CODES, problem_code
from lucy_api.packs.base import Availability, State
from lucy_api.packs.context import Call
from lucy_api.packs.probes import PROBE_TTL_SECONDS, GuardedHttp, ProbeCache, ProviderLocks
from lucy_api.packs.registry import probe_all
from lucy_api.packs.service import Capabilities
from lucy_api.sessions.scope import SessionScope


class Counting(Gadget):
    """A gadget that records how many times the network-shaped probe ran."""

    def __init__(self, pack_id: str = "gadget") -> None:
        super().__init__(pack_id)
        self.probes = 0

    async def probe(self, context: object) -> Availability:
        self.probes += 1
        return await super().probe(context)


def _capabilities(pack: Gadget, *, probes: ProbeCache | None = None) -> Capabilities:
    return Capabilities((pack,), probes=probes)


def _context(capabilities: Capabilities) -> Any:
    return capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )


async def test_a_second_probe_within_the_ttl_does_not_hit_the_pack_again() -> None:
    gadget = Counting()
    capabilities = _capabilities(gadget)
    context = _context(capabilities)

    first = await capabilities.probe(context)
    second = await capabilities.probe(context)

    assert gadget.probes == 1
    assert first.get("gadget") is not None
    assert second.get("gadget") is not None
    assert first.get("gadget").availability.state is State.ready
    assert second.get("gadget").availability.state is State.ready


async def test_an_expired_entry_is_probed_again() -> None:
    clock = [0.0]
    gadget = Counting()
    capabilities = _capabilities(gadget, probes=ProbeCache(ttl_seconds=15, now=lambda: clock[0]))
    context = _context(capabilities)

    await capabilities.probe(context)
    clock[0] = 15.0
    await capabilities.probe(context)

    assert gadget.probes == 2


async def test_forgetting_one_pack_leaves_the_others() -> None:
    left = Counting("left")
    right = Counting("right")
    capabilities = Capabilities((left, right))
    context = _context(capabilities)
    await capabilities.probe(context)
    capabilities.forget_probes("acct_a", "personal", "left")
    await capabilities.probe(context)

    assert left.probes == 2
    assert right.probes == 1


async def test_another_account_or_profile_does_not_share_the_cache() -> None:
    gadget = Counting()
    capabilities = _capabilities(gadget)
    first = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    other_account = capabilities.context_for(
        SessionScope(account_id="acct_b", profile="personal", session_id="ses_b")
    )
    other_profile = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="work", session_id="ses_c")
    )
    await capabilities.probe(first)
    await capabilities.probe(other_account)
    await capabilities.probe(other_profile)
    assert gadget.probes == 3


async def test_operations_still_run_locally_on_a_cache_hit() -> None:
    gadget = Counting()
    capabilities = _capabilities(gadget)
    context = _context(capabilities)
    await capabilities.probe(context)
    catalogue = await capabilities.probe(context)
    bound = catalogue.get("gadget")
    assert bound is not None
    assert bound.operations
    assert gadget.probes == 1


def test_the_default_ttl_is_short_enough_that_a_missed_invalidation_ages_out() -> None:
    assert 1 <= PROBE_TTL_SECONDS <= 30


class _RoutingHttp:
    """Holds the Spotify lock's inner call until released; other audiences return at once."""

    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self.started = started
        self.release = release
        self.slow_calls = 0
        self.fast_calls = 0

    async def request(self, call: Call) -> Any:
        return (await self.request_response(call)).body

    async def request_response(self, call: Call) -> Any:
        if call.audience == "spotify-api":
            self.slow_calls += 1
            self.started.set()
            await self.release.wait()
        else:
            self.fast_calls += 1
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True}, body={"ok": True})


async def test_two_calls_to_the_same_audience_are_serialised() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    inner = _RoutingHttp(started, release)
    http = GuardedHttp(inner, ProviderLocks(), account_id="acct_a", profile="personal")
    call = Call(method="GET", url="https://music.test/v1", audience="spotify-api")

    async def first() -> Any:
        return await http.request_response(call)

    async def second() -> Any:
        await started.wait()
        return await http.request_response(call)

    leading = asyncio.create_task(first())
    await started.wait()
    trailing = asyncio.create_task(second())
    await asyncio.sleep(0.01)
    assert not trailing.done()
    release.set()
    await leading
    await trailing
    assert inner.slow_calls == 2


async def test_calls_to_different_audiences_do_not_wait_on_each_other() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    inner = _RoutingHttp(started, release)
    http = GuardedHttp(inner, ProviderLocks(), account_id="acct_a", profile="personal")
    leading = asyncio.create_task(
        http.request_response(Call(method="GET", url="https://a.test/", audience="spotify-api"))
    )
    await started.wait()
    await http.request_response(Call(method="GET", url="https://b.test/", audience="memory-api"))
    assert inner.fast_calls == 1
    assert not leading.done()
    release.set()
    await leading


async def test_a_credential_unavailable_response_drops_the_cached_probe() -> None:
    dropped: list[str] = []
    http = GuardedHttp(
        _StatusHttp(502, {"type": "https://example.test/problems/credential-unavailable"}),
        ProviderLocks(),
        account_id="acct_a",
        profile="personal",
        on_disconnect=lambda: dropped.append("yes"),
    )
    response = await http.request_response(
        Call(method="GET", url="https://music.test/v1/player/devices", audience="spotify-api")
    )
    assert response.status_code == 502
    assert problem_code(response.json()) in CREDENTIAL_CODES
    assert dropped == ["yes"]


async def test_a_502_that_is_not_a_missing_credential_does_not_drop_the_cache() -> None:
    dropped: list[str] = []
    http = GuardedHttp(
        _StatusHttp(502, {"type": "https://example.test/problems/upstream-refused"}),
        ProviderLocks(),
        account_id="acct_a",
        profile="personal",
        on_disconnect=lambda: dropped.append("yes"),
    )
    await http.request_response(
        Call(method="GET", url="https://music.test/v1", audience="spotify-api")
    )
    assert dropped == []


async def test_an_ordinary_outage_does_not_drop_the_cache() -> None:
    dropped: list[str] = []
    http = GuardedHttp(
        _StatusHttp(503, {"type": "https://example.test/problems/unavailable"}),
        ProviderLocks(),
        account_id="acct_a",
        profile="personal",
        on_disconnect=lambda: dropped.append("yes"),
    )
    await http.request_response(
        Call(method="GET", url="https://music.test/v1", audience="spotify-api")
    )
    assert dropped == []


async def test_forgetting_a_profile_drops_every_pack() -> None:
    left = Counting("left")
    right = Counting("right")
    capabilities = Capabilities((left, right))
    context = _context(capabilities)
    await capabilities.probe(context)
    capabilities.forget_probes("acct_a", "personal")
    await capabilities.probe(context)
    assert left.probes == 2
    assert right.probes == 2


async def test_changing_a_setting_invalidates_cached_probes() -> None:
    from lucy_api.clients.settings import FakeSettingsPackClient, Setting
    from lucy_api.packs.settings import SettingsPack

    gadget = Counting()
    fake = FakeSettingsPackClient(
        [
            Setting(
                "lucy",
                "max_llm_turns",
                12,
                kind="integer",
                summary="Maximum model rounds.",
                description="Stops runaway turns.",
                source="default",
                bounds={"minimum": 1, "maximum": 100},
                scope="account",
            )
        ]
    )
    capabilities = Capabilities([SettingsPack("https://settings.test", client=fake), gadget])
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a",
            profile="personal",
            session_id="ses_a",
            permission_mode="auto",
        )
    )
    await capabilities.probe(context)
    assert gadget.probes == 1
    await capabilities.execute(
        {
            "steps": [
                {
                    "id": "set",
                    "op": "settings.set",
                    "input": {"namespace": "lucy", "key": "max_llm_turns", "value": 20},
                }
            ]
        },
        context,
    )
    await capabilities.probe(context)
    assert gadget.probes == 2


async def test_request_takes_the_same_per_audience_lock() -> None:
    inner = _StatusHttp(200, {"ok": True})
    http = GuardedHttp(inner, ProviderLocks(), account_id="acct_a", profile="personal")
    body = await http.request(Call(method="GET", url="https://music.test/", audience="spotify-api"))
    assert body == {"ok": True}


async def test_a_malformed_502_body_does_not_drop_the_cache() -> None:
    dropped: list[str] = []

    class Broken:
        status_code = 502

        def json(self) -> Any:
            raise ValueError("nope")

    class Inner:
        async def request(self, call: Call) -> Any:
            del call
            return None

        async def request_response(self, call: Call) -> Any:
            del call
            return Broken()

    http = GuardedHttp(
        Inner(),
        ProviderLocks(),
        account_id="acct_a",
        profile="personal",
        on_disconnect=lambda: dropped.append("yes"),
    )
    await http.request_response(
        Call(method="GET", url="https://music.test/", audience="spotify-api")
    )
    assert dropped == []


async def test_without_a_disconnect_hook_a_missing_credential_is_still_returned() -> None:
    http = GuardedHttp(
        _StatusHttp(502, {"type": "https://example.test/problems/credential-unavailable"}),
        ProviderLocks(),
        account_id="acct_a",
        profile="personal",
    )
    response = await http.request_response(
        Call(method="GET", url="https://music.test/", audience="spotify-api")
    )
    assert response.status_code == 502


async def test_direct_probe_all_without_a_cache_still_probes_every_time() -> None:
    gadget = Counting()
    context = Capabilities((gadget,)).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    context.probes = None
    await probe_all((gadget,), context)
    await probe_all((gadget,), context)
    assert gadget.probes == 2


class _StatusHttp:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body

    async def request(self, call: Call) -> Any:
        del call
        return self.body

    async def request_response(self, call: Call) -> Any:
        del call
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)
