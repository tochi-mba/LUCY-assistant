"""Where the live state comes from, and what happens when part of it does not arrive.

The state block is assembled from half a dozen places: the agent roster, the shared task
journal, the workspace, the capability probes, the memory topic index, the approvals queue.
Every one of them is a separate system, and on any given turn one of them may be slow,
restarting, or simply not deployed.

**A missing source must never fail the turn.** An assistant that cannot answer a question
because it could not read its own task journal is worse than one that answers the question
and mentions that it could not see the journal. So each source is gathered independently, a
failure costs that one group, and the omission is reported to the model in the block's own
`trouble` group. That is precisely where "something I usually see is missing" belongs, and
it is the same place repeated tool failures are already reported.

This is the rule the rest of the hub follows for capabilities -- something unavailable
degrades to a warning while everything else still registers -- applied here because context
assembly runs on every single turn, which makes it the place where a hard dependency hurts
most.

## Why every source has the same shape

One generic `Source[T]` rather than six differently named Protocols. It costs a little
self-description at the definition -- the method is `fetch`, not `agents` -- and buys the
thing that matters more: a single gather path with no per-group branching, so "what happens
when this source fails" has exactly one answer and exactly one test. The field name on
`Sources` carries the meaning at every call site.

A source given as `None` is not deployed, and is silently absent: there is nothing to
report about a subsystem the operator never installed, and saying so every turn would be
noise the model has to read past. A source that is present and *fails* is reported, because
that is a change from the previous turn and it explains a gap the model can otherwise see
but not account for.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from lucy_api.context.types import FailureSnapshot, LiveState, PendingSnapshot

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from lucy_api.context.types import (
        BudgetSnapshot,
        CapabilitySnapshot,
        SessionSnapshot,
        TaskSnapshot,
        TopicSnapshot,
        WorkSnapshot,
        WorkspaceSnapshot,
    )


class Source[T](Protocol):
    """One group of live state, fetched for one session."""

    async def fetch(self, session_id: str) -> T: ...


@dataclass(frozen=True, slots=True)
class Sources:
    """The systems the state block is built from. Any of them may be absent."""

    in_flight: Source[Sequence[WorkSnapshot]] | None = None
    tasks: Source[Sequence[TaskSnapshot]] | None = None
    workspace: Source[WorkspaceSnapshot | None] | None = None
    capabilities: Source[Sequence[CapabilitySnapshot]] | None = None
    topics: Source[Sequence[TopicSnapshot]] | None = None
    pending: Source[PendingSnapshot] | None = None


async def _fetch[T](
    name: str,
    source: Source[T] | None,
    session_id: str,
    fallback: T,
    trouble: list[FailureSnapshot],
) -> T:
    """Fetch one group. Absence is silent; failure is reported and costs only this group.

    `BaseException` is deliberately not caught. A cancellation means the turn has been
    abandoned, and swallowing it here would leave the assembler building a prompt that
    nobody is waiting for.
    """
    if source is None:
        return fallback
    try:
        return await source.fetch(session_id)
    except Exception as exc:
        trouble.append(
            FailureSnapshot(
                operation=name,
                count=1,
                detail=f"unavailable ({type(exc).__name__}); omitted from this turn",
            )
        )
        return fallback


@dataclass(frozen=True, slots=True)
class StateRequest:
    """The facts the caller already holds, which no source needs to be asked for."""

    now: datetime
    session: SessionSnapshot
    budget: BudgetSnapshot
    failures: Sequence[FailureSnapshot] = ()


async def gather_live_state(request: StateRequest, sources: Sources) -> LiveState:
    """Build one turn's live state, concurrently, tolerating any source being absent.

    Concurrently because these are six independent round trips and the turn waits on all of
    them; serially this would be the slowest thing between a person pressing enter and the
    first token coming back.
    """
    trouble: list[FailureSnapshot] = list(request.failures)
    session_id = request.session.id

    nothing_running: Sequence[WorkSnapshot] = ()
    no_tasks: Sequence[TaskSnapshot] = ()
    no_capabilities: Sequence[CapabilitySnapshot] = ()
    no_topics: Sequence[TopicSnapshot] = ()
    no_workspace: WorkspaceSnapshot | None = None

    in_flight, tasks, workspace, capabilities, topics, pending = await asyncio.gather(
        _fetch("in_flight", sources.in_flight, session_id, nothing_running, trouble),
        _fetch("journal", sources.tasks, session_id, no_tasks, trouble),
        _fetch("workspace", sources.workspace, session_id, no_workspace, trouble),
        _fetch("capabilities", sources.capabilities, session_id, no_capabilities, trouble),
        _fetch("memory", sources.topics, session_id, no_topics, trouble),
        _fetch("pending", sources.pending, session_id, PendingSnapshot(), trouble),
    )

    return LiveState(
        now=request.now,
        session=request.session,
        budget=request.budget,
        in_flight=tuple(in_flight),
        tasks=tuple(tasks),
        topics=tuple(topics),
        capabilities=tuple(capabilities),
        workspace=workspace,
        pending=pending,
        failures=tuple(trouble),
    )
