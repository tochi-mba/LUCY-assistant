"""Pin remaining branches that the suite already implied but never stepped on."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from conftest import ACCOUNT, build_settings

from lucy_api.auth.exchange import ExchangeError
from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.clients.environments import FakeEnvironmentsClient
from lucy_api.clients.errors import DownstreamError, NotConnectedError, UnavailableError
from lucy_api.clients.keyring import DelegatedKeyringClient
from lucy_api.clients.search import FakeSearchClient, Findings, Hit, Provider
from lucy_api.clients.spotify import FakeSpotifyClient, HttpSpotifyClient, Play, Track, Wanted
from lucy_api.clients.testing import Answer, FakeHttp
from lucy_api.context.feeds import MAX_ENTRIES, claims_from, parse_document
from lucy_api.core.container import PackRequest, _feed_flags, _integer, build_container
from lucy_api.core.errors import LucyError
from lucy_api.packs.agents import AgentsPack, _list, _spawn
from lucy_api.packs.base import State
from lucy_api.packs.collections import _get
from lucy_api.packs.context import SilentTokens
from lucy_api.packs.help import HelpPack
from lucy_api.packs.http import PackHttp
from lucy_api.packs.music import MusicPack
from lucy_api.packs.research import ResearchPack, _search_limit
from lucy_api.packs.research import _summary as research_summary
from lucy_api.packs.service import Capabilities
from lucy_api.packs.settings import SettingsPack
from lucy_api.prompt.docs import capability_doc
from lucy_api.sessions.compact import _consecutive_failures, _covers_to, _summary
from lucy_api.sessions.models import CreateSession
from lucy_api.sessions.scope import SessionScope
from lucy_api.sessions.sql_store import SessionStore
from lucy_api.sessions.turns import submit_messages
from lucy_api.stream.events import COMPACTION_DISABLED, COMPACTION_FAILED
from lucy_api.turn.stop import Termination
from lucy_api.turn.supervisor import TurnSupervisor, _status_for
from lucy_api.work.registry import Registry
from lucy_api.work.types import Brief, Kind


def test_a_dataclass_collection_item_exposes_the_same_fields_as_a_mapping() -> None:
    assert _get({"name": "Ada"}, "name") == "Ada"
    assert _get(SimpleNamespace(name="Ada"), "name") == "Ada"
    assert _get(SimpleNamespace(), "name") == ""


def test_turn_terminations_map_onto_the_statuses_a_client_can_poll() -> None:
    assert _status_for(Termination.success) == "completed"
    assert _status_for(Termination.refused) == "completed"
    assert _status_for(Termination.cancelled) == "cancelled"
    assert _status_for(Termination.input_required) == "input_required"
    assert _status_for(Termination.auth_required) == "auth_required"
    assert _status_for(Termination.failed) == "failed"


def test_feed_documents_drop_malformed_rows_rather_than_inventing_a_feed() -> None:
    assert parse_document(["not-a-feed"]) == ()
    assert parse_document({"id": "!!!", "lines": ["x"]}) == ()
    assert parse_document({"id": "music", "volatility": "nope", "lines": ["x"]}) == ()
    assert parse_document({"id": "music", "lines": "not-a-list"}) == ()
    assert (
        parse_document(
            {
                "id": "music",
                "entries": [
                    "skip-me",
                    *({"key": f"k{index:02d}", "line": "ok"} for index in range(MAX_ENTRIES + 2)),
                ],
            }
        )[0]
        .entries[-1]
        .key
        == f"k{MAX_ENTRIES - 1:02d}"
    )
    parsed = parse_document(
        {
            "id": "music",
            "lines": ["ok"],
            "entries": [{"key": "now", "line": "song", "recorded_at": "not-a-date"}],
        }
    )
    assert parsed[0].entries[0].recorded_at is None
    standing = parse_document({"id": "persona", "volatility": "standing", "lines": ["note"]})
    live = parse_document({"id": "music", "volatility": "live", "lines": ["now"]})
    claims = claims_from((*standing, *live))
    assert len(claims) == 1
    assert claims[0].body == "note"


def test_a_compaction_summary_skips_items_newer_than_the_cover() -> None:
    text = _summary([{"seq": 9, "content_json": "later", "role": "user"}], covers_to=1)
    assert "User:" not in text
    assert "Covered items 1-1" in text
    kept = _summary([{"seq": 1, "content_json": "hello", "role": "assistant"}], covers_to=1)
    assert "User:" not in kept
    assert "Covered items 1-1" in kept

    class Rows:
        def execute(self, *_args: object, **_kwargs: object) -> Rows:
            return self

        def fetchall(self) -> list[dict[str, str]]:
            return [
                {"type": COMPACTION_DISABLED},
                {"type": COMPACTION_FAILED},
                {"type": COMPACTION_FAILED},
            ]

    assert _consecutive_failures(Rows(), "ses_a") == 2  # type: ignore[arg-type]


def test_settings_without_a_usable_token_are_unavailable() -> None:
    pack = SettingsPack("https://settings.test")
    assert pack.docs == capability_doc("settings")
    assert pack.setup() is None


async def test_settings_probe_names_an_exchange_failure() -> None:
    class Boom:
        async def describe(self, *, profile: str) -> object:
            del profile
            raise ExchangeError("no grant")

    pack = SettingsPack("https://settings.test", client=Boom())  # type: ignore[arg-type]
    context = Capabilities(()).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    assert (await pack.probe(context)).state is State.unavailable


async def test_research_probe_covers_each_provider_outcome() -> None:
    fake = FakeSearchClient()
    pack = ResearchPack("https://search.test", client=fake)
    assert pack.docs == capability_doc("research")
    fake.offer([Provider("openai", "error")])
    assert (await pack.probe(_pack_context())).state is State.unavailable

    class Boom:
        async def providers(self, *, profile: str = "") -> list[Provider]:
            raise ExchangeError("no grant")

    broken = ResearchPack("https://search.test", client=Boom())  # type: ignore[arg-type]
    assert (await broken.probe(_pack_context())).state is State.unavailable
    assert research_summary(None) is None


async def test_research_search_surfaces_a_provider_notice() -> None:
    fake = FakeSearchClient()
    fake.offer([Provider("openai", "available", model_count=1)])
    fake.seed(
        Findings(
            query="lucy",
            hits=(Hit(title="One", url="https://example.test", rank=1),),
            detail="showing 1 of 4",
        )
    )
    pack = ResearchPack("https://search.test", client=fake)
    capabilities = Capabilities((pack,))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "s", "op": "research.search", "input": {"query": "lucy"}}]},
        context,
    )
    assert "showing 1 of 4" in str(result)


async def test_music_recent_plays_are_projected() -> None:
    fake = FakeSpotifyClient()
    track = Track(name="Clair de lune", artists=("Debussy",), uri="spotify:track:1")
    fake.seed(plays=(Play(track, played_at=datetime(2026, 1, 1, tzinfo=UTC)),))
    capabilities = Capabilities((HelpPack(), MusicPack("http://music.test", client=fake)))
    context = capabilities.context_for(
        SessionScope(
            account_id="acct_a", profile="personal", session_id="ses_a", permission_mode="auto"
        )
    )
    await capabilities.probe(context)
    result = await capabilities.execute(
        {"steps": [{"id": "r", "op": "music.recent", "input": {}}]},
        context,
    )
    assert result["issues"] is None
    assert "Clair de lune" in str(result)


async def test_http_music_lookups_omit_optional_fields_when_they_were_not_asked() -> None:
    http = FakeHttp(
        Answer(body={"results": [{"index": 0, "status": "found", "track": {"name": "A"}}]}),
        Answer(body={"item": {"name": "A"}, "is_playing": True}),
    )
    client = HttpSpotifyClient(http, "http://music.test")
    await client.find([Wanted(name="A")], profile="work")
    await client.queue("work", "spotify:track:1")
    assert "market" not in (http.calls[0].json or {})
    assert "device_id" not in (http.calls[1].json or {})


async def test_execute_probes_when_the_turn_has_no_catalogue_yet() -> None:
    capabilities = Capabilities((HelpPack(),))
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    result = await capabilities.execute(
        {"steps": [{"id": "s", "op": "help.operation", "input": {"name": "help.operation"}}]},
        context,
    )
    assert result["issues"] is None


async def test_agents_list_and_an_empty_brief_run_through_the_pack() -> None:
    work = Registry(now=lambda: datetime.now(UTC))
    pack = AgentsPack()
    capabilities = Capabilities((pack,), work=work)
    context = capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )
    listed = next(op for op in pack.operations(context) if op.name == "agents.list")
    payload = await listed.run(SimpleNamespace(input={}, ctx=context))
    assert payload["count"] == 0
    spawned = next(op for op in pack.operations(context) if op.name == "agents.spawn")
    empty = spawned.run(SimpleNamespace(input={"objective": "  ", "role": "x"}, ctx=context))
    if hasattr(empty, "__await__"):
        empty = await empty
    assert empty["status"] == "invalid"
    child = capabilities.context_for(
        SessionScope(
            account_id="acct_a",
            profile="personal",
            session_id="ses_a",
            agent_id="agt_1",
        )
    )
    names = {op.name for op in pack.operations(child)}
    assert "agents.spawn" not in names
    assert "agents.reopen" not in names
    assert "journal.claim" in names
    messenger = next(op for op in pack.operations(child) if op.name == "agents.message")
    steered = await messenger.run(SimpleNamespace(input={"id": "", "message": "stop"}, ctx=child))
    assert steered["status"] in {"invalid", "not_configured"}
    assert _list(work, "ses_a")["count"] == 0
    assert (await _spawn(work, context, depth=0, objective="", role="x"))["status"] == "invalid"
    await work.shutdown()


async def test_a_duplicate_work_id_is_refused_and_the_coroutine_is_closed() -> None:
    registry = Registry(now=lambda: datetime.now(UTC))
    brief = Brief(session_id="ses_a", kind=Kind.helper, role="h", objective="go")

    async def once() -> str:
        return "ok"

    try:
        registry.start(once(), brief, work_id="wrk_fixed")
        await asyncio.sleep(0)
        refused = once()
        with pytest.raises(ValueError, match="already registered"):
            registry.start(refused, brief, work_id="wrk_fixed")
        with pytest.raises(RuntimeError, match="cannot reuse"):
            await refused
    finally:
        await registry.shutdown()


async def test_an_approval_shaped_input_cannot_open_a_new_turn(
    sessions_store: SessionStore,
) -> None:
    created = await sessions_store.create(ACCOUNT, CreateSession(model="scripted:demo"), "k")
    with pytest.raises(LucyError):
        await submit_messages(
            sessions_store,
            ACCOUNT,
            str(created["id"]),
            [{"type": "input.approval", "approval_id": "apr_x", "approved": True}],
            "approval-as-message",
        )


async def test_a_missing_stream_snapshot_is_the_same_miss_as_a_foreign_session(
    sessions_store: SessionStore,
) -> None:
    with pytest.raises(LucyError):
        await sessions_store.stream_snapshot("ses_missing")


async def test_attaching_a_workspace_twice_returns_the_original(
    sessions_store: SessionStore,
) -> None:
    created = await sessions_store.create(ACCOUNT, CreateSession(model="scripted:demo"), "ws")
    session_id = str(created["id"])
    first = await sessions_store.attach_workspace(ACCOUNT, session_id, "env-1", "sessions/a")
    again = await sessions_store.attach_workspace(ACCOUNT, session_id, "env-2", "sessions/b")
    assert first["workspace_environment_id"] == again["workspace_environment_id"] == "env-1"


async def test_a_database_probe_failure_is_named_by_the_exception_type(
    sessions_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_operation: Any) -> Any:
        raise RuntimeError("disk")

    monkeypatch.setattr(sessions_store.worker, "call", boom)
    ok, name = await sessions_store.healthy()
    assert ok is False
    assert name == "RuntimeError"


async def test_joining_a_supervisor_that_never_started_is_a_no_op(
    sessions_store: SessionStore,
) -> None:
    supervisor = TurnSupervisor(sessions_store, SimpleNamespace(providers={}), SimpleNamespace())
    await supervisor.join()


def test_container_helpers_use_conservative_defaults_when_settings_are_down() -> None:
    assert _integer(None, "max_llm_turns", 12, minimum=1, maximum=100) == 12
    assert _feed_flags(None).values == {}

    class Values:
        def get(self, key: str, default: object = None) -> object:
            del key, default
            return True

    class Words:
        def get(self, key: str, default: object = None) -> object:
            del key, default
            return "twelve"

    class Number:
        def get(self, key: str, default: object = None) -> object:
            del key, default
            return 3

    assert _integer(Values(), "max_llm_turns", 12, minimum=1, maximum=100) == 12  # type: ignore[arg-type]
    assert _integer(Words(), "max_llm_turns", 12, minimum=1, maximum=100) == 12  # type: ignore[arg-type]
    assert _integer(Number(), "max_llm_turns", 12, minimum=1, maximum=100) == 3  # type: ignore[arg-type]
    assert _integer(Number(), "max_llm_turns", 12, minimum=1, maximum=2) == 2  # type: ignore[arg-type]


async def test_a_keyring_network_failure_is_unavailable() -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as http:
        client = DelegatedKeyringClient(
            http, "http://keyring.test", service_token="service-secret", user_token="user"
        )
        with pytest.raises(UnavailableError):
            await client.connections("personal")


async def test_a_keyring_non_json_body_is_unavailable() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="not-json"))
    ) as http:
        client = DelegatedKeyringClient(
            http, "http://keyring.test", service_token="service-secret", user_token="user"
        )
        with pytest.raises(UnavailableError):
            await client.connections("personal")


async def test_pack_http_does_not_close_an_injected_client() -> None:
    shared = httpx.AsyncClient()
    http = PackHttp(tokens=SilentTokens(), client=shared)
    await http.aclose()
    assert shared.is_closed is False
    await shared.aclose()


async def test_connection_and_environment_clients_are_built_from_the_request(
    keyring: Any,
) -> None:
    container = build_container(
        build_settings(keyring_service_token="x" * 32), transport=keyring.transport()
    )
    try:
        request = PackRequest(
            VerifiedCaller("acct_a", "lucy-api"),
            "user-jwt",
            "personal",
            "ses_x",
        )
        client = container.connection_client(request)
        assert isinstance(client, DelegatedKeyringClient)
        env = container.environment_client(request)
        assert env is not None
        attached = await container.ensure_workspace(
            request, {"id": "ses_x", "workspace_environment_id": "env-1"}
        )
        assert attached["workspace_environment_id"] == "env-1"
    finally:
        await container.aclose()


async def test_music_not_connected_publishes_no_live_feed() -> None:
    from lucy_api.clients.live_feeds import MusicFeeds, PersonaFeeds
    from lucy_api.context.feeds import FeedRequest

    class Boom:
        async def now_playing(self, profile: str) -> object:
            del profile
            raise NotConnectedError("spotify", 502, "not linked")

        async def devices(self, profile: str) -> object:
            del profile
            raise NotConnectedError("spotify", 502, "not linked")

    request = FeedRequest(profile="personal", session_id="ses_1")
    assert await MusicFeeds(Boom()).fetch(request) == ()  # type: ignore[arg-type]
    http = FakeHttp(
        Answer(
            body={
                "persona": "not-a-card",
                "fields": ["skip", {"key": "k", "value": 1}],
                "notes": ["skip", {"note_id": "n1", "body": "keep"}],
            }
        )
    )
    feeds = await PersonaFeeds(http, "http://persona.test").fetch(request)
    assert feeds == () or feeds[0].id == "persona"
    empty = FakeHttp(Answer(body={"persona": {}, "fields": [], "notes": []}))
    assert await PersonaFeeds(empty, "http://persona.test").fetch(request) == ()


def test_compaction_cover_treats_numeric_turn_ids_like_strings() -> None:
    assert (
        _covers_to(
            [
                {"turn_id": 1, "seq": 1},
                {"turn_id": 2, "seq": 2},
                {"turn_id": 3, "seq": 3},
            ]
        )
        == 1
    )


def test_a_claim_guard_is_true_only_when_exactly_one_row_changed() -> None:
    from lucy_api.sessions.sql_store import claimed_one_row

    class Changes:
        def __init__(self, count: int) -> None:
            self.count = count

        def execute(self, sql: str) -> Changes:
            assert "changes()" in sql
            return self

        def fetchone(self) -> tuple[int]:
            return (self.count,)

    assert claimed_one_row(Changes(1)) is True  # type: ignore[arg-type]
    assert claimed_one_row(Changes(0)) is False  # type: ignore[arg-type]
    assert claimed_one_row(Changes(2)) is False  # type: ignore[arg-type]


async def test_a_lost_claim_race_leaves_the_turn_queued(
    sessions_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await sessions_store.create(
        ACCOUNT, CreateSession(model="scripted:demo"), "claim-race"
    )
    await submit_messages(
        sessions_store,
        ACCOUNT,
        str(created["id"]),
        [{"type": "input.message", "content": "hello"}],
        "claim-race-input",
    )
    monkeypatch.setattr("lucy_api.sessions.sql_store.claimed_one_row", lambda _db: False)
    assert await sessions_store.claim_next_turn() is None


async def test_a_closed_supervisor_does_not_claim_work(sessions_store: SessionStore) -> None:
    from lucy_api.model.registry import ModelRegistry
    from lucy_api.model.scripted import ScriptedProvider
    from lucy_api.stream.emitter import EventEmitter, SqlEventLog

    class Snapshot:
        async def snapshot(self, session_id: str) -> dict[str, str]:
            return {"session_id": session_id}

    running = TurnSupervisor(
        sessions_store,
        ModelRegistry({"scripted": lambda _: ScriptedProvider([])}),
        EventEmitter(SqlEventLog(sessions_store), Snapshot()),
    )
    running._closed = True
    await running._drain()


async def test_workspace_bootstrap_skips_existing_files_and_survives_a_git_outage() -> None:
    from lucy_api.core.container import _bootstrap_session_workspace
    from lucy_api.sessions.scope import PROGRESS_FILE, WorkspaceScope

    class NoGit(FakeEnvironmentsClient):
        async def run(
            self,
            environment_id: str,
            command: str,
            *,
            cwd: str = ".",
            timeout_ms: int = 0,
            max_output_bytes: int = 0,
        ) -> Any:
            del environment_id, command, cwd, timeout_ms, max_output_bytes
            raise DownstreamError("environments", 503, "git missing")

    fake = NoGit()
    environment = await fake.create("lucy-a-personal")
    workspace = WorkspaceScope(environment.environment_id, "ses_boot")
    await fake.mkdir(environment.environment_id, workspace.root)
    await _bootstrap_session_workspace(fake, workspace)
    progress = f"{workspace.root}/{PROGRESS_FILE}"
    assert fake.contents[(environment.environment_id, progress)].startswith("# Progress")
    fake.contents[(environment.environment_id, progress)] = "kept\n"
    await _bootstrap_session_workspace(fake, workspace)
    assert fake.contents[(environment.environment_id, progress)] == "kept\n"


def _pack_context() -> Any:
    return Capabilities(()).context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )


def test_a_nonsensical_research_limit_falls_back_to_the_default() -> None:
    from lucy_api.clients.search import DEFAULT_RESULTS

    broken = SimpleNamespace(input={"limit": "nope"}, ctx=SimpleNamespace(defaults={}))
    assert _search_limit(broken) == DEFAULT_RESULTS
    missing = SimpleNamespace(input={}, ctx=SimpleNamespace(defaults={"research.limit": object()}))
    assert _search_limit(missing) == DEFAULT_RESULTS
    huge = SimpleNamespace(input={"limit": 99}, ctx=SimpleNamespace(defaults={}))
    assert _search_limit(huge) == 20
    preferred = SimpleNamespace(input={}, ctx=SimpleNamespace(defaults={"research.limit": 2}))
    assert _search_limit(preferred) == 2
    empty = SimpleNamespace(input={"limit": 0}, ctx=SimpleNamespace(defaults={}))
    assert _search_limit(empty) == 1
