"""Wire models: the shapes the session surface puts on the network.

Separate from :mod:`lucy_api.sessions.models` on purpose. Those are the validated vocabulary
the store reads and writes; these are the documents that go out, and the two are free to move
apart -- the HTTP contract is public, its operation ids are MCP tool names, and it has to be
able to grow a field without the storage layer having an opinion about it.

Three things here are decisions rather than plumbing.

**No response names an account.** A row out of the store carries ``account_id``; these models
do not declare it, so validating a row through one drops it. That is the isolation invariant
made structural rather than remembered: there is no field for somebody else's id and no field
for your own, so a session document is safe to paste into a bug report.

**The page envelope forbids extra fields and the resources do not.** Opposite defaults for
opposite jobs. A resource is built from a database row that has more columns than the
contract exposes, and dropping them is the point. The envelope is built from a literal we
control, so an unexpected key in it means the pagination contract has drifted, and drifting
silently is exactly the failure cursor paging exists to prevent.

**Paging is cursor-only.** ``limit``, ``order``, ``after`` and ``before``, and never a page
number: an append-only log grows while it is being read, and offset paging over a growing
collection both duplicates rows and skips them. A cursor that does not name a row in the
collection is a 404 rather than an empty page, because "your bookmark is stale" and "there is
nothing there" are different answers and only one of them means reload.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, JsonValue

from lucy_api.sessions.models import Cursor

SelectionDep = Annotated[Cursor, Query()]
"""The cursor, as query parameters, from the model the domain already takes.

Writing the four out by hand would work exactly once: the first time somebody adds a field
to :class:`~lucy_api.sessions.models.Cursor`, the HTTP surface would silently stop offering
it."""


class Page[T](BaseModel):
    """One window onto a collection, with the two ids needed to ask for the next."""

    model_config = ConfigDict(extra="forbid")

    data: list[T]
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class SessionResource(BaseModel):
    """A conversation, as a client sees it.

    ``parent_session_id`` and ``forked_from_item`` are only set on a fork, and together they
    name exactly where the two conversations diverged. ``workspace_environment_id`` is
    null on a fork: branching a conversation does not share the original files.
    """

    id: str
    profile: str
    title: str
    status: str
    model: str
    thinking_config: str
    persona: str
    parent_session_id: str | None = None
    forked_from_item: str | None = None
    workspace_environment_id: str | None = None
    workspace_rel: str | None = None
    harness_version: str
    input_policy: str
    durability_mode: str
    permission_mode: str
    incognito: bool
    created_at: float
    updated_at: float
    archived_at: float | None = None
    input_tokens: int
    output_tokens: int
    cost_micros: int


class ItemResource(BaseModel):
    """One entry in a transcript.

    ``seq`` is when it was written and only ever climbs; ``parent_id`` is what it answers and
    is allowed to fork, which is how an edited message and its regeneration end up as
    siblings rather than one looking like a reply to the other.
    """

    id: str
    session_id: str
    seq: int
    parent_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    type: str
    role: str
    content: JsonValue = None
    tokens: int
    created_at: float


class TurnResource(BaseModel):
    """One unit of work, and how it ended.

    ``termination`` and ``stop_reason`` are separate because they answer different questions:
    why the hub stopped, and what the provider said about its own generation.
    ``error_max_iterations`` can be resumed and a ``refusal`` cannot, so a client that
    collapsed them into one field would offer a retry for something that will never succeed.

    ``cancel_requested`` can be true on a turn that is still running: a stop is cooperative
    while there is work in flight worth preserving.
    """

    id: str
    session_id: str
    status: str
    termination: str | None = None
    stop_reason: str | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error_code: str | None = None
    input_tokens: int
    output_tokens: int
    cost_micros: int
    iterations: int
    cancel_requested: bool
