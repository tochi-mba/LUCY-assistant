"""Optional decisions affect useful runtime paths without becoming authority."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.hub.test_memory_index import Listing, card
from tests.hub.test_turn_loop import PLAN, Prompts, executor, ok_result
from weftai.decisions import Answer, Answers, noul

from lucy_api.context.types import Trust
from lucy_api.decide import Decisions
from lucy_api.decide.types import CAPABILITIES, MEMORY, RECOVERY
from lucy_api.decide.uses import (
    MAX_CANDIDATES,
    RECOVERY_NOTICE,
    Recovery,
    rank_relevant,
    suggest_capabilities,
)
from lucy_api.memory.index import MemoryIndex
from lucy_api.memory.topics import Topic
from lucy_api.model.scripted import ScriptedProvider, plans, speaks
from lucy_api.packs.base import Availability, Bound, Catalogue, State
from lucy_api.packs.service import Capabilities
from lucy_api.settings.policy import TurnPolicy
from lucy_api.turn.loop import Turn, run_turn


class Answerer:
    def __init__(self, selected=(), probability=0.99):
        self.selected = selected
        self.probability = probability
        self.calls = []

    async def decide(self, state, questions):
        self.calls.append((state, questions))
        return Answers(
            [Answer(q.id, "noul", q.id in self.selected, self.probability) for q in questions]
        )


def decisions(answerer=None, *, enabled=None, shadow=False, **kwargs):
    result = Decisions(
        answerer or Answerer(),
        enabled=enabled if enabled is not None else [CAPABILITIES.id, MEMORY.id, RECOVERY.id],
        shadow=shadow,
        **kwargs,
    )
    result.request_text = "Find jazz and my music preferences"
    return result


def catalogue(count=9):
    return Catalogue(
        tuple(
            Bound(
                SimpleNamespace(id=f"cap{i}", summary=f"Capability {i}"),
                Availability(State.ready),
                (SimpleNamespace(name=f"cap{i}.read"),),
            )
            for i in range(count)
        )
    )


@pytest.mark.parametrize("shadow", [False, True])
async def test_capability_preload_preserves_existing_tools_and_session_recency(shadow):
    original = catalogue()
    service = Capabilities(())
    before, deferred = service.bound_for(original, "session")
    target = deferred[-1]
    answerer = Answerer([f"c{target[3:]}"])
    updated = await suggest_capabilities(decisions(answerer, shadow=shadow), original)
    after, remaining = service.bound_for(updated, "session")
    assert {item.pack.id for item in before} <= {item.pack.id for item in after}
    assert service.recent("session") == ()
    assert (target not in remaining) is (not shadow)
    assert len(answerer.calls) == 1
    assert original.suggested == ()


@pytest.mark.parametrize(
    "selected,probability", [([], 0.99), (["c8"], 0.4), (["c0", "c1", "c2"], 0.99)]
)
async def test_uncertain_or_overbroad_capability_selection_keeps_discovery(selected, probability):
    original = catalogue()
    assert (
        await suggest_capabilities(decisions(Answerer(selected, probability)), original)
    ).suggested == ()


async def test_disabled_missing_request_empty_or_excessive_candidates_do_not_call():
    answerer = Answerer()
    for context, candidates in [
        (decisions(answerer, enabled=[]), catalogue()),
        (decisions(answerer), Catalogue()),
        (decisions(answerer), catalogue(MAX_CANDIDATES + 1)),
    ]:
        assert await suggest_capabilities(context, candidates) == candidates
    context = decisions(answerer)
    context.request_text = ""
    assert await suggest_capabilities(context, catalogue()) == catalogue()
    assert answerer.calls == []


async def test_unavailable_capabilities_never_reach_the_decider():
    original = catalogue(2)
    blocked = replace(original.bound[1], availability=Availability(State.not_connected))
    answerer = Answerer(["c1"])
    updated = await suggest_capabilities(
        decisions(answerer), Catalogue((original.bound[0], blocked))
    )
    assert len(answerer.calls[0][1]) == 1
    assert updated.suggested == ()


@pytest.mark.parametrize("shadow", [False, True])
async def test_memory_relevance_promotes_without_deleting_or_exposing_untrusted_topics(shadow):
    listing = Listing(
        (
            card(id="old", importance=0.1),
            card(id="recent", importance=0.9),
            card(id="secret", title="UNTRUSTED_SENTINEL", trust="untrusted"),
        )
    )
    answerer = Answerer(["m1"])
    context = decisions(answerer, shadow=shadow)
    snapshots = await MemoryIndex(listing, "personal", limit=1, decide=context).fetch("session")
    assert snapshots[0].id == ("recent" if shadow else "old")
    assert "UNTRUSTED_SENTINEL" not in answerer.calls[0][0]
    assert ("showing 1 of 2" in snapshots[0].index_notice) is (not shadow)
    await MemoryIndex(listing, "personal", limit=1, decide=context).fetch("session")
    assert len(answerer.calls) == 1  # same input in a later model round is cached


async def test_incognito_and_zero_limit_do_not_send_memory_to_laya():
    answerer = Answerer()
    context = decisions(answerer)
    listing = Listing((card(),))
    assert await MemoryIndex(listing, "personal", incognito=True, decide=context).fetch("s") == ()
    assert listing.profile == ""
    await MemoryIndex(listing, "personal", limit=0, decide=context).fetch("s")
    assert answerer.calls == []


async def test_memory_empty_disabled_large_and_missing_request_fall_back():
    topics = tuple(
        Topic(id=str(i), key=str(i), title="t", trust=Trust.stated)
        for i in range(MAX_CANDIDATES + 1)
    )
    answerer = Answerer()
    for context, candidates in [
        (decisions(answerer), ()),
        (decisions(answerer), topics),
        (decisions(answerer, enabled=[]), topics[:2]),
    ]:
        assert await rank_relevant(context, candidates) == candidates
    context = decisions(answerer)
    context.request_text = ""
    assert await rank_relevant(context, topics[:2]) == topics[:2]
    assert answerer.calls == []


async def test_memory_without_positive_judgments_preserves_the_original_order():
    topics = (
        Topic(id="first", key="first", title="First", trust=Trust.stated),
        Topic(id="second", key="second", title="Second", trust=Trust.stated),
    )
    answerer = Answerer()
    assert await rank_relevant(decisions(answerer), topics) == topics
    assert len(answerer.calls) == 1


@pytest.mark.parametrize("shadow", [False, True])
async def test_recovery_nudges_once_after_consecutive_failures_and_never_stops_a_turn(shadow):
    answerer = Answerer(["same_obstacle"])
    recovery = Recovery(decisions(answerer, shadow=shadow))
    assert await recovery.observe(["first failure"]) == ""
    assert await recovery.observe([]) == ""
    assert await recovery.observe(["failure"]) == ""
    assert await recovery.observe(["failure again"]) == ("" if shadow else RECOVERY_NOTICE)
    assert await recovery.observe(["failure three"]) == ""
    assert len(answerer.calls) == 1


async def test_recovery_disabled_or_uncertain_does_nothing():
    assert await Recovery(decisions(enabled=[])).observe(["failed"]) == ""
    recovery = Recovery(decisions(Answerer(["same_obstacle"], 0.2)))
    await recovery.observe(["failed"])
    assert await recovery.observe(["failed again"]) == ""


@pytest.mark.parametrize("shadow", [False, True])
async def test_real_loop_applies_only_an_advisory_notice(shadow):
    prompts = Prompts()
    provider = ScriptedProvider([plans(PLAN), plans(PLAN), speaks("Here is the blocker.")])
    result = await run_turn(
        Turn(
            provider=provider,
            assemble=prompts.assemble,
            execute=executor(ok_result(status="error", error="unreachable")),
            recovery=Recovery(decisions(Answerer(["same_obstacle"]), shadow=shadow)),
        )
    )
    assert result.text == "Here is the blocker."
    assert len(provider.requests) == 3
    assert (RECOVERY_NOTICE in prompts.notices[-1]) is (not shadow)


async def test_cache_reuses_answers_after_budget_and_redacts_credentials():
    answerer = Answerer(["yes"])
    context = decisions(answerer, max_per_turn=1)
    questions = [noul("yes", "Is it relevant?")]
    state = "sk-" + "a" * 40
    first = await context.ask(MEMORY, state, questions)
    assert await context.ask(MEMORY, state, questions) is first
    assert (await context.ask(MEMORY, "different", questions)).empty
    assert state not in answerer.calls[0][0]
    assert len(answerer.calls) == context.spent == 1


def test_policy_reads_flags_and_clamps_limits():
    defaults = TurnPolicy.from_resolved({})
    assert not defaults.decisions
    assert defaults.decision_shadow_mode
    policy = TurnPolicy.from_resolved(
        {
            "decisions": True,
            "decision_shadow_mode": False,
            "decision_capabilities": False,
            "decision_memory": False,
            "decision_recovery": True,
            "decision_timeout_ms": 99999,
            "decision_max_per_turn": -1,
        }
    )
    assert policy.decisions
    assert not policy.decision_shadow_mode
    assert not policy.decision_capabilities
    assert not policy.decision_memory
    assert policy.decision_recovery
    assert policy.decision_timeout_ms == 5000
    assert policy.decision_max_per_turn == 1
    assert not TurnPolicy.from_resolved({"decisions": "yes"}).decisions


async def test_rendered_memory_preserves_relevance_order_and_honest_counts():
    from tests.hub.test_context_state import Chars, a_state

    from lucy_api.context.state import render_state

    listing = Listing(
        (
            card(id="old", title="Older music", importance=0.1),
            card(id="recent", title="Recent work", importance=0.9),
            card(id="other", title="Other", importance=0.5),
        )
    )
    context = decisions(Answerer(["m2"]))
    snapshots = await MemoryIndex(listing, "personal", limit=2, decide=context).fetch("s")
    rendered = render_state(a_state(topics=tuple(snapshots)), limit=4000, counter=Chars()).body
    assert rendered.index("Older music") < rendered.index("Recent work")
    assert "showing 2 of 3 topics" in rendered
    assert "notes.search" in rendered
