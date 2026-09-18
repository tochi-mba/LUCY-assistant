"""The always-bound help operations: list, setup, bind, docs, skills, and one operation's schema."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from weftai.operation import define_operation
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.mcp.skills import CATALOGUE
from lucy_api.packs.base import Availability, Bound, Catalogue, SetupPlan, SetupStep, State
from lucy_api.packs.context import Call, NoBrokerError, PackContext, SilentTokens
from lucy_api.packs.help import (
    HelpPack,
    _docs,
    _list,
    _operation,
    _pack_docs,
    _setup,
    _skill,
    _skills,
    _use,
)
from lucy_api.packs.http import DownstreamUnavailableError, NullHttp
from lucy_api.packs.service import Capabilities, as_loop_result
from lucy_api.sessions.scope import SessionScope


class Gadget:
    """A fictional capability used to drive every help-handler branch."""

    def __init__(
        self,
        pack_id: str = "gadget",
        *,
        state: State = State.not_connected,
        docs: Path | None = None,
        setup_plan: SetupPlan | None = None,
    ) -> None:
        self.id = pack_id
        self.title = pack_id.title()
        self.summary = f"The {pack_id} capability."
        self._state = state
        self._docs = docs
        self._setup = setup_plan

    @property
    def docs(self) -> Path | None:
        return self._docs

    def permissions(self) -> tuple[Any, ...]:
        return ()

    def setup(self) -> SetupPlan | None:
        return self._setup

    async def probe(self, _context: object) -> Availability:
        return Availability(
            state=self._state,
            detail="need a link",
            connect_url="https://example.test/connect",
            missing_scopes=("read",),
        )

    def operations(self, _context: object) -> tuple[Any, ...]:
        async def run(_run: object) -> dict[str, bool]:
            return {"ok": True}

        return (
            define_operation(
                {
                    "name": f"{self.id}.ping",
                    "description": "Ping the gadget.",
                    "input": object_schema({}),
                    "output": value(object_schema({"ok": string_schema()})),
                    "effects": "read",
                    "run": run,
                }
            ),
        )


def _run(context: object, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(ctx=context, input=payload)


def _context(capabilities: Capabilities) -> PackContext:
    return capabilities.context_for(
        SessionScope(account_id="acct_a", profile="personal", session_id="ses_a")
    )


async def test_help_declares_no_docs_permissions_or_setup() -> None:
    pack = HelpPack()
    assert pack.docs is None
    assert pack.permissions() == ()
    assert pack.setup() is None
    assert (await pack.probe(_context(Capabilities((pack,))))).state is State.ready


async def test_list_is_empty_when_the_catalogue_has_not_been_probed() -> None:
    context = _context(Capabilities((HelpPack(),)))
    listed = await _list(_run(context))
    assert listed == {"capabilities": []}


async def test_setup_without_a_catalogue_is_the_same_unknown_as_a_missing_id() -> None:
    result = await _setup(_run(_context(Capabilities((HelpPack(),))), id="gadget"))
    assert result["status"] == "unknown"


async def test_a_setup_with_an_empty_summary_keeps_the_connection_sentence() -> None:
    plan = SetupPlan(
        summary="",
        steps=(SetupStep(id="connect", kind="oauth", title="Connect", description="Go."),),
    )
    capabilities = Capabilities((HelpPack(), Gadget(setup_plan=plan)))
    context = _context(capabilities)
    await capabilities.probe(context)
    result = await _setup(_run(context, id="gadget"))
    assert result["status"] == "connection_required"
    assert result["message"]


async def test_setup_names_an_unknown_capability_rather_than_inventing_a_link() -> None:
    capabilities = Capabilities((HelpPack(),))
    context = _context(capabilities)
    await capabilities.probe(context)
    result = await _setup(_run(context, id="gadget"))
    assert result["status"] == "unknown"
    assert "gadget" in result["message"]


async def test_setup_says_ready_when_the_capability_is_already_connected() -> None:
    capabilities = Capabilities((HelpPack(), Gadget(state=State.ready)))
    context = _context(capabilities)
    await capabilities.probe(context)
    result = await _setup(_run(context, id="gadget"))
    assert result["status"] == "ready"
    assert "already connected" in result["message"]


async def test_setup_without_an_offer_returns_the_state_and_the_plan_summary() -> None:
    plan = SetupPlan(summary="An operator has to deploy this first.")
    capabilities = Capabilities((HelpPack(), Gadget(state=State.disabled, setup_plan=plan)))
    context = _context(capabilities)
    await capabilities.probe(context)
    result = await _setup(_run(context, id="gadget"))
    assert result["status"] == "disabled"
    assert result["message"] == plan.summary


async def test_setup_of_an_offerable_capability_returns_the_connection_body() -> None:
    plan = SetupPlan(
        summary="Open the link.",
        steps=(SetupStep(id="connect", kind="oauth", title="Connect", description="Approve."),),
    )
    capabilities = Capabilities((HelpPack(), Gadget(setup_plan=plan)))
    context = _context(capabilities)
    await capabilities.probe(context)
    result = await _setup(_run(context, id="gadget"))
    assert result["status"] == "connection_required"
    assert result["connect_url"] == "https://example.test/connect"
    assert result["scopes"] == ["read"]
    assert result["message"] == "Open the link."


async def test_use_refuses_an_unknown_or_unusable_capability() -> None:
    capabilities = Capabilities((HelpPack(), Gadget()))
    context = _context(capabilities)
    await capabilities.probe(context)
    missing = await _use(_run(context, id="missing"))
    blocked = await _use(_run(context, id="gadget"))
    assert missing["bound"] is False
    assert blocked["bound"] is False
    assert "not usable" in blocked["message"]


async def test_use_binds_a_ready_capability_for_the_rest_of_the_session() -> None:
    capabilities = Capabilities((HelpPack(), Gadget(state=State.ready)))
    context = _context(capabilities)
    await capabilities.probe(context)
    result = await _use(_run(context, id="gadget"))
    assert result["bound"] is True
    assert "gadget" in context.bound_ids


async def test_docs_window_the_help_manual_and_a_capability_file(tmp_path: Path) -> None:
    manual = tmp_path / "gadget.md"
    manual.write_text("Gadget docs.\nUse gadget.ping.", encoding="utf-8")
    capabilities = Capabilities((HelpPack(), Gadget(state=State.ready, docs=manual)))
    context = _context(capabilities)
    await capabilities.probe(context)
    help_page = await _docs(_run(context, topic="help", offset=0, limit=40))
    packed = await _docs(_run(context, topic="gadget"))
    missing = await _docs(_run(context, topic="nope"))
    assert help_page["text"].startswith("# Help")
    assert "showing" in help_page["showing"]
    assert packed["text"] == "Gadget docs.\nUse gadget.ping."
    assert "no capability" in missing["text"]


async def test_docs_without_a_file_fall_back_to_the_pack_summary() -> None:
    capabilities = Capabilities((HelpPack(), Gadget(state=State.ready)))
    context = _context(capabilities)
    await capabilities.probe(context)
    assert _pack_docs(context, "gadget") == "The gadget capability."
    assert _pack_docs(context, "help") == HelpPack.summary


async def test_operation_returns_the_schema_or_says_the_name_is_unknown() -> None:
    capabilities = Capabilities((HelpPack(), Gadget(state=State.ready)))
    context = _context(capabilities)
    await capabilities.probe(context)
    found = await _operation(_run(context, name="gadget.ping"))
    missing = await _operation(_run(context, name="gadget.explode"))
    empty = await _operation(SimpleNamespace(ctx=SimpleNamespace(catalogue=None), input={}))
    assert found["name"] == "gadget.ping"
    assert found["examples"] == []
    assert "no operation" in missing["error"]
    assert "no operation" in empty["error"]


async def test_a_summary_line_includes_the_detail_when_there_is_one() -> None:
    pack = HelpPack()
    plain = Bound(pack=pack, availability=Availability(state=State.ready)).summary_line()
    detailed = Bound(
        pack=pack, availability=Availability(state=State.ready, detail="always available")
    ).summary_line()
    assert "[ready]" in plain
    assert "always available" not in plain
    assert "always available" in detailed


async def test_null_http_and_silent_tokens_fail_closed_without_a_secret() -> None:
    http = NullHttp()
    call = Call(method="GET", url="http://example.test", audience="example-tool")
    with pytest.raises(DownstreamUnavailableError):
        await http.request(call)
    with pytest.raises(DownstreamUnavailableError):
        await http.request_response(call)
    with pytest.raises(NoBrokerError):
        await SilentTokens().token_for("example-tool")
    context = PackContext(
        account_id="acct_a",
        profile="personal",
        session_id="ses_a",
        http=http,
        tokens=SilentTokens(),
    )
    first = context.limit("example-tool")
    again = context.limit("example-tool")
    assert first is again


def test_offerable_is_only_the_capabilities_that_still_need_setup() -> None:
    catalogue = Catalogue(
        bound=(
            Bound(pack=HelpPack(), availability=Availability(state=State.ready)),
            Bound(
                pack=Gadget(),
                availability=Availability(state=State.not_connected),
            ),
        )
    )
    assert [item.pack.id for item in catalogue.offerable()] == ["gadget"]


def test_remembering_a_capability_twice_does_not_duplicate_it() -> None:
    capabilities = Capabilities((HelpPack(),))
    capabilities.remember_use("ses_a", "help")
    capabilities.remember_use("ses_a", "help")
    capabilities.remember_use("ses_a", "gadget")
    assert capabilities.recent("ses_a") == ("help", "gadget")


def test_a_non_dict_loop_result_is_empty_text_rather_than_an_exception() -> None:
    result = as_loop_result("plain text")
    assert result["text"] == ""
    assert result["issues"] is None
    assert result["steps"] == []


async def test_bound_for_keeps_ready_capabilities() -> None:
    capabilities = Capabilities((HelpPack(), Gadget(state=State.ready)))
    context = _context(capabilities)
    catalogue = await capabilities.probe(context)
    bound, _deferred = capabilities.bound_for(catalogue, context.session_id)
    assert {item.pack.id for item in bound} >= {"help", "gadget"}


async def test_skills_list_the_same_corpus_an_mcp_client_loads() -> None:
    listed = await _skills(_run(_context(Capabilities((HelpPack(),)))))
    names = [row["name"] for row in listed["skills"]]
    assert names == [skill.name for skill in CATALOGUE]
    talking = next(row for row in listed["skills"] if row["name"] == "talking")
    assert talking["title"]
    assert talking["summary"]


async def test_skill_windows_named_docs_and_names_unknowns() -> None:
    context = _context(Capabilities((HelpPack(),)))
    page = await _skill(_run(context, name="talking", offset=0, limit=40))
    assert page["name"] == "talking"
    assert page["text"].startswith("# Talking")
    assert "showing" in page["showing"]
    assert page["total"] > len(page["text"])
    missing = await _skill(_run(context, name="not-a-skill"))
    assert "no skill named 'not-a-skill'" in missing["error"]
    assert "talking" in missing["error"]
