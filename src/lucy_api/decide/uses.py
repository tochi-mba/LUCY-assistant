"""Bounded judgments over eligible capabilities, trusted topics, and failed attempts."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from weftai.decisions import Gate, noul

from lucy_api.context.scrub import scrub
from lucy_api.decide.types import CAPABILITIES, MEMORY, RECOVERY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lucy_api.decide import Decisions
    from lucy_api.memory.topics import Topic
    from lucy_api.packs.base import Catalogue

MAX_CANDIDATES = 16
MAX_SUGGESTED = 2
THRESHOLD = 0.85
RECOVERY_NOTICE = (
    "Recent attempts may be hitting the same obstacle. Check the reported results before "
    "retrying; change the approach or explain what is blocking progress."
)


async def suggest_capabilities(decide: Decisions, catalogue: Catalogue) -> Catalogue:
    """Preload up to two eligible capabilities without executing or granting anything."""
    candidates = catalogue.ready()
    if not decide.request_text or CAPABILITIES.id not in decide.enabled or not candidates:
        return catalogue
    if len(candidates) > MAX_CANDIDATES:
        await decide.skipped(CAPABILITIES, "candidate_limit")
        return catalogue
    questions = [
        noul(f"c{i}", f"Is capability {i} needed for the request?") for i in range(len(candidates))
    ]
    state = json.dumps(
        {
            "request": decide.request_text,
            "available_capabilities": [
                {"index": i, "summary": item.pack.summary} for i, item in enumerate(candidates)
            ],
        },
        ensure_ascii=False,
    )
    answers = await decide.ask(CAPABILITIES, state, questions)
    gate: Gate[bool] = Gate(THRESHOLD, fail_open=False)
    selected = tuple(
        item.pack.id
        for i, item in enumerate(candidates)
        if gate.decide(answers, f"c{i}", answers.noul(f"c{i}"))
    )
    # Broad requests keep ordinary discovery rather than guessing which two to drop.
    if len(selected) > MAX_SUGGESTED:
        return catalogue
    if selected:
        await decide.disagreed(CAPABILITIES, decided=",".join(selected), fallback="discovery")
    return replace(catalogue, suggested=selected) if decide.live(CAPABILITIES) else catalogue


async def rank_relevant(decide: Decisions, topics: Sequence[Topic]) -> tuple[Topic, ...]:
    """Stable promotion of relevant trusted topics; no topic is deleted or relabelled."""
    original = tuple(topics)
    if not decide.request_text or MEMORY.id not in decide.enabled or not original:
        return original
    if len(original) > MAX_CANDIDATES:
        await decide.skipped(MEMORY, "candidate_limit")
        return original
    questions = [
        noul(f"m{i}", f"Is topic {i} relevant to the current request?")
        for i in range(len(original))
    ]
    state = json.dumps(
        {
            "request": decide.request_text,
            "reported_topics": [
                {"index": i, "title": topic.title, "summary": topic.summary}
                for i, topic in enumerate(original)
            ],
        },
        ensure_ascii=False,
    )
    answers = await decide.ask(MEMORY, state, questions)
    gate: Gate[bool] = Gate(THRESHOLD, fail_open=False)
    preferred = {
        i for i in range(len(original)) if gate.decide(answers, f"m{i}", answers.noul(f"m{i}"))
    }
    ordered = tuple(topic for i, topic in enumerate(original) if i in preferred) + tuple(
        topic for i, topic in enumerate(original) if i not in preferred
    )
    if ordered != original:
        # Counts only: never store memory titles or identifiers in a decision event.
        await decide.disagreed(MEMORY, decided="relevance_order", fallback="recency_order")
    return ordered if decide.live(MEMORY) else original


class Recovery:
    """One advisory nudge after consecutive failures; successful work resets the comparison."""

    def __init__(self, decide: Decisions) -> None:
        self.decide = decide
        self.previous = ""
        self.advised = False

    async def observe(self, failures: Sequence[str]) -> str:
        if self.advised or RECOVERY.id not in self.decide.enabled:
            return ""
        current = scrub(json.dumps(list(failures), ensure_ascii=False)).text if failures else ""
        previous, self.previous = self.previous, current
        if not previous or not current:
            return ""
        answers = await self.decide.ask(
            RECOVERY,
            json.dumps({"previous_failures": previous, "current_failures": current}),
            [
                noul(
                    "same_obstacle",
                    "Do these attempts fail because of the same underlying obstacle?",
                )
            ],
        )
        gate: Gate[bool] = Gate(0.9, fail_open=False)
        if not gate.decide(answers, "same_obstacle", answers.noul("same_obstacle")):
            return ""
        self.advised = True
        await self.decide.disagreed(RECOVERY, decided="reconsider", fallback="continue")
        return RECOVERY_NOTICE if self.decide.live(RECOVERY) else ""
