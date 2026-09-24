"""Production composition: the configured HTTP adapter, settings, and emitted decisions."""

from conftest import build_settings
from keyring_client.testing import FakeKeyring
from settings_client.testing import FakeSettingsClient
from tests.hub.test_laya_decisions import Answerer
from weftai.decisions import noul
from weftai.providers.laya import LayaDecider

from lucy_api.auth.verifier import VerifiedCaller
from lucy_api.core.container import PackRequest, build_container
from lucy_api.decide.types import CAPABILITIES, MEMORY
from lucy_api.sessions.models import CreateSession


class ToyPack:
    docs = None

    def __init__(self, number):
        self.id = f"cap{number}"
        self.title = self.id
        self.summary = f"Use capability {number}"

    def setup(self):
        return None

    def permissions(self):
        from lucy_api.packs.base import Permission

        return (
            Permission(
                id=f"{self.id}.write",
                title="Change something",
                description="Change something",
                risk="write",
                covers=(f"{self.id}.run",),
            ),
        )

    async def probe(self, context):
        from lucy_api.packs.base import Availability, State

        return Availability(State.ready)

    def operations(self, context):
        from weftai.operation import define_operation
        from weftai.schema.spec import object_schema
        from weftai.schema.types import value

        return (
            define_operation(
                {
                    "name": f"{self.id}.run",
                    "description": self.summary,
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self.run,
                }
            ),
        )

    async def run(self, args, context):
        return {"changed": True}


async def test_supervisor_preloads_first_round_but_still_requires_permission(tmp_path):
    import json

    from tests.hub.test_laya_decisions import decisions
    from tests.hub.test_turn_supervisor import ACCOUNT, session, supervisor

    from lucy_api.context.build import Live
    from lucy_api.model.scripted import ScriptedProvider, plans
    from lucy_api.packs.service import Capabilities
    from lucy_api.sessions.scope import SessionScope
    from lucy_api.sessions.sql_store import SessionStore
    from lucy_api.sessions.turns import submit_messages
    from lucy_api.store.worker import SqlWorker
    from lucy_api.turn.supervisor import PreparedTurn

    worker = SqlWorker(str(tmp_path / "laya.sqlite3"))
    store = SessionStore(worker)
    await store.initialize()
    caps = Capabilities(tuple(ToyPack(i) for i in range(9)))
    provider = ScriptedProvider(
        [plans({"steps": [{"id": "change", "op": "cap8.run", "input": {}}]})]
    )
    running = supervisor(store, provider, capabilities=caps)
    try:
        conversation = await session(store)
        queued = await submit_messages(
            store,
            ACCOUNT,
            conversation,
            [{"type": "input.message", "content": "Use capability 8"}],
            "message",
        )
        context = caps.context_for(
            SessionScope(account_id=ACCOUNT, profile="personal", session_id=conversation)
        )
        answerer = Answerer(["c8"])
        context.decide = decisions(answerer)
        running.authorize(str(queued["id"]), PreparedTurn(pack_context=context, live=Live()))
        running.wake()
        await running.join()
        assert len(answerer.calls) == 1
        assert "Use capability 8" in answerer.calls[0][0]
        assert "cap8.run" in json.dumps(provider.requests[0].plan_schema)
        row = await store.turn(ACCOUNT, queued["id"])
        assert row["status"] == "input_required"
        assert caps.recent(conversation) == ()
    finally:
        await running.aclose()
        await worker.aclose()


async def test_container_uses_configured_adapter_and_real_settings():
    keyring = FakeKeyring()
    container = build_container(
        build_settings(laya_base_url="http://localhost:8010"), transport=keyring.transport()
    )
    try:
        assert isinstance(container.decider, LayaDecider)
        await container.preferences.aclose()
        container.preferences = FakeSettingsClient(
            {"lucy": {"decisions": True, "decision_shadow_mode": False, "decision_memory": False}}
        )
        answerer = Answerer(["needed"])
        container.decider = answerer
        await container.start()
        session = await container.store.create("acct_a", CreateSession(), "create")
        prepared = await container.prepare_turn(
            PackRequest(
                caller=VerifiedCaller(account_id="acct_a", audience="lucy-api"),
                user_token="verified",
                profile="personal",
                session_id=session["id"],
            ),
            session,
        )
        decide = prepared.pack_context.decide
        assert decide.live(CAPABILITIES)
        assert not decide.live(MEMORY)
        assert prepared.live.sources.topics.decide is decide
        answer = await decide.ask(CAPABILITIES, "play music", [noul("needed", "Is it needed?")])
        assert answer.noul("needed")
        events = await container.store.records("acct_a", session["id"], "events")
        assert any(row["type"] == "lucy.decision.made" for row in events)
    finally:
        await container.aclose()
