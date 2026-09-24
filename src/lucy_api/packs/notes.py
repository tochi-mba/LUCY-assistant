"""Facts and lessons about the person, named `notes` so the model never sees a service.

Persona is the assistant's voice. Memory is what it remembers. Account is the pinned
fields a person asked to keep in view. This pack is the one place the model reads those
as "notes", because a model that has to pick between three services to answer "what do we
know about them" will pick wrong, and a model that never sees a service cannot.

The three stores stay separate lists. Memory retrieval scores and account pinning are not
the same ranking, and merging them would hide a pinned name under a bm25 that meant
something else. `notes.search` is memory only. `notes.aboutMe` returns `blocks`, `facts`
and `account` as three keys.

Untrusted notes stay out of search: Memory-api already excludes them from retrieval. This
pack never offers a way around that. Confirming is an explicit operation, because permanence
is exactly what makes a memory store worth attacking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import integer_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.auth.exchange import ExchangeError
from lucy_api.clients.errors import DownstreamError
from lucy_api.clients.memory import (
    AUDIENCE,
    DEFAULT_LIMIT,
    Draft,
    HttpMemoryClient,
    MemoryClient,
    as_dict,
)
from lucy_api.clients.user import AUDIENCE as USER_AUDIENCE
from lucy_api.clients.user import HttpUserClient
from lucy_api.clients.user import as_dict as account_dict
from lucy_api.packs.base import Availability, Permission, SetupPlan, State
from lucy_api.packs.collections import NOTE
from lucy_api.packs.context import NoBrokerError
from lucy_api.packs.http import DownstreamError as TransportError
from lucy_api.prompt.docs import capability_doc

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.packs.context import PackContext

INCOGNITO = "this session is incognito: notes are neither read nor written"


class NotesPack:
    """Memory, as a product word, never as a host."""

    id = "notes"
    title = "Notes"
    summary = "Remember, search, confirm, correct and forget facts about the person."

    def __init__(
        self,
        base_url: str,
        *,
        audience: str = AUDIENCE,
        user_base_url: str = "",
        user_audience: str = USER_AUDIENCE,
        client: MemoryClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self.user_base_url = user_base_url.rstrip("/")
        self.user_audience = user_audience
        self._override = client

    @property
    def docs(self) -> str | Path | None:
        return capability_doc(self.id)

    def permissions(self) -> Sequence[Permission]:
        return (
            Permission(
                id="notes.write",
                title="Remember and change notes about you",
                description="Write, correct or confirm a note.",
                risk="write",
                covers=(
                    "notes.setFact",
                    "notes.remember",
                    "notes.confirm",
                    "notes.correct",
                ),
            ),
            Permission(
                id="notes.erase",
                title="Forget a note",
                description=(
                    "Remove a remembered note. Auto mode still asks, unless you already "
                    "allowed this."
                ),
                risk="destructive",
                covers=("notes.forget",),
            ),
        )

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:
        try:
            await self._client(context).blocks(profile=context.profile)
        except (NoBrokerError, ExchangeError):
            return Availability(state=State.unavailable, detail="cannot act for this person yet")
        except (DownstreamError, TransportError):
            return Availability(state=State.unavailable, detail="notes could not be reached")
        return Availability(state=State.ready, detail="connected")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:
        # weftai operation names are namespaced lowerCamelCase (`notes.aboutMe`), not
        # snake_case. The plan's `notes.about_me` would be refused at bind time.
        del context
        return (
            define_operation(
                {
                    "name": "notes.aboutMe",
                    "description": (
                        "Who this person is: pinned memory blocks, the highest-ranked "
                        "memories, and pinned account facts as a separate list. Do not "
                        "merge the lists or treat their order as one ranking (about me, "
                        "profile, identity, preferences)."
                    ),
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": self._about_me,
                }
            ),
            define_operation(
                {
                    "name": "notes.schema",
                    "description": (
                        "What kinds of note exist and when to use each (fact, episode, "
                        "procedure, summary; account, profile, session)."
                    ),
                    "input": object_schema({}),
                    "output": value(object_schema({})),
                    "effects": "read",
                    "run": _schema,
                }
            ),
            define_operation(
                {
                    "name": "notes.search",
                    "description": (
                        "Find remembered notes worth putting in front of the model. This "
                        "is memory only — pinned account fields are on notes.aboutMe, not "
                        "here, because the scores are not comparable. Untrusted and "
                        "forgotten notes are excluded (search, recall, remember, lookup)."
                    ),
                    "input": object_schema(
                        {
                            "query": string_schema().describe(
                                "Keywords, or empty for the top notes."
                            ),
                            "limit": integer_schema().optional(),
                        }
                    ),
                    # A collection rather than an opaque value: the model is shown one line
                    # per note, a later step can say `$notes[0,2]` against the whole result
                    # rather than against the lines it happened to see, and `note.count`,
                    # `note.filter` and the rest come with it for nothing.
                    "output": NOTE,
                    "effects": "read",
                    "run": self._search,
                }
            ),
            define_operation(
                {
                    "name": "notes.openTopic",
                    "description": (
                        "Expand one memory topic from the live index into its notes. Use "
                        "after reading the index; do not expand several on speculation."
                    ),
                    "input": object_schema(
                        {
                            "topic_id": string_schema().describe(
                                "The topic id from the live memory index."
                            )
                        }
                    ),
                    "output": NOTE,
                    "effects": "read",
                    "run": self._open_topic,
                }
            ),
            define_operation(
                {
                    "name": "notes.setFact",
                    "description": (
                        "Record a durable fact about the person (preference, constraint, "
                        "identity). Use remember for a one-off episode."
                    ),
                    "input": object_schema(
                        {
                            "title": string_schema().describe("A short name for the fact."),
                            "body": string_schema().describe(
                                "The fact, in the person's words if possible."
                            ),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._set_fact,
                }
            ),
            define_operation(
                {
                    "name": "notes.remember",
                    "description": (
                        "Keep something from this conversation as an episode. It stays "
                        "with this session unless they ask to promote it."
                    ),
                    "input": object_schema(
                        {
                            "title": string_schema().describe("What this episode is."),
                            "body": string_schema().describe(
                                "What happened, and why it was worth keeping."
                            ),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._remember,
                }
            ),
            define_operation(
                {
                    "name": "notes.confirm",
                    "description": (
                        "Mark an untrusted note as vouched-for so retrieval may use it. "
                        "Confirm only what the person themselves confirmed."
                    ),
                    "input": object_schema(
                        {"memory_id": string_schema().describe("The note to confirm.")}
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._confirm,
                }
            ),
            define_operation(
                {
                    "name": "notes.correct",
                    "description": (
                        "Replace a note with a corrected one, keeping the history. Never "
                        "delete-then-add: that throws away that it used to be true."
                    ),
                    "input": object_schema(
                        {
                            "memory_id": string_schema().describe("The note to supersede."),
                            "title": string_schema().describe("The corrected title."),
                            "body": string_schema().describe("The corrected body."),
                        }
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._correct,
                }
            ),
            define_operation(
                {
                    "name": "notes.forget",
                    "description": (
                        "Forget one note. It is hidden immediately and erased after the "
                        "grace period, and can be restored until then."
                    ),
                    "input": object_schema(
                        {"memory_id": string_schema().describe("The note to forget.")}
                    ),
                    "output": value(object_schema({})),
                    "effects": "write",
                    "run": self._forget,
                }
            ),
        )

    def _client(self, context: PackContext) -> MemoryClient:
        return self._override or HttpMemoryClient(
            context.http, self.base_url, audience=self.audience
        )

    def _account(self, context: PackContext) -> HttpUserClient | None:
        if not self.user_base_url:
            return None
        return HttpUserClient(context.http, self.user_base_url, audience=self.user_audience)

    async def _about_me(self, run: RunContext[PackContext]) -> dict[str, Any]:
        if run.ctx.incognito:
            return {
                "status": "incognito",
                "message": INCOGNITO,
                "blocks": [],
                "facts": [],
                "account": [],
            }
        client = self._client(run.ctx)
        blocks = await client.blocks(profile=run.ctx.profile)
        facts = await client.search(
            profile=run.ctx.profile, session_id=run.ctx.session_id, limit=DEFAULT_LIMIT
        )
        return {
            "blocks": [{"label": block.label, "body": block.body} for block in blocks],
            "facts": [as_dict(note) for note in facts],
            "account": await self._pinned_account(run.ctx),
        }

    async def _pinned_account(self, context: PackContext) -> list[dict[str, Any]]:
        """Pinned account facts, or empty. A user-api miss must not blank the memories."""
        client = self._account(context)
        if client is None:
            return []
        try:
            return [account_dict(fact) for fact in await client.pinned(profile=context.profile)]
        except (NoBrokerError, ExchangeError, DownstreamError, TransportError):
            return []

    async def _search(self, run: RunContext[PackContext]) -> list[dict[str, Any]]:
        """The notes themselves. The runtime labels, counts and references them.

        An incognito session returns nothing and says so through a notice rather than by
        returning a differently-shaped result: a caller that has to branch on the shape of
        a result is a caller that will forget to.
        """
        if run.ctx.incognito:
            run.notice(INCOGNITO)
            return []
        query = str(run.input.get("query") or "")
        limit = int(run.input.get("limit") or DEFAULT_LIMIT)
        notes = await self._client(run.ctx).search(
            query,
            profile=run.ctx.profile,
            session_id=run.ctx.session_id,
            limit=max(1, min(limit, 20)),
        )
        return [as_dict(note) for note in notes]

    async def _open_topic(self, run: RunContext[PackContext]) -> list[dict[str, Any]]:
        """The memories inside one topic. The index exists so this is paid for on purpose."""
        if run.ctx.incognito:
            run.notice(INCOGNITO)
            return []
        notes = await self._client(run.ctx).topic_memories(
            str(run.input.get("topic_id") or ""),
            profile=run.ctx.profile,
        )
        return [as_dict(note) for note in notes]

    async def _set_fact(self, run: RunContext[PackContext]) -> dict[str, Any]:
        return await self._write(run, kind="fact")

    async def _remember(self, run: RunContext[PackContext]) -> dict[str, Any]:
        return await self._write(run, kind="episode")

    async def _write(self, run: RunContext[PackContext], *, kind: str) -> dict[str, Any]:
        refused = _incognito(run)
        if refused is not None:
            return refused
        note = await self._client(run.ctx).remember(
            Draft(
                title=str(run.input.get("title") or ""),
                body=str(run.input.get("body") or ""),
                kind=kind,
                profile=run.ctx.profile,
                session_id=run.ctx.session_id,
            )
        )
        return as_dict(note)

    async def _confirm(self, run: RunContext[PackContext]) -> dict[str, Any]:
        refused = _incognito(run)
        if refused is not None:
            return refused
        return as_dict(
            await self._client(run.ctx).confirm(
                str(run.input.get("memory_id") or ""), profile=run.ctx.profile
            )
        )

    async def _correct(self, run: RunContext[PackContext]) -> dict[str, Any]:
        refused = _incognito(run)
        if refused is not None:
            return refused
        return as_dict(
            await self._client(run.ctx).correct(
                str(run.input.get("memory_id") or ""),
                str(run.input.get("title") or ""),
                str(run.input.get("body") or ""),
                profile=run.ctx.profile,
            )
        )

    async def _forget(self, run: RunContext[PackContext]) -> dict[str, Any]:
        refused = _incognito(run)
        if refused is not None:
            return refused
        return as_dict(
            await self._client(run.ctx).forget(
                str(run.input.get("memory_id") or ""), profile=run.ctx.profile
            )
        )


def _incognito(run: RunContext[PackContext]) -> dict[str, Any] | None:
    if run.ctx.incognito:
        return {"status": "incognito", "message": INCOGNITO}
    return None


async def _schema(run: RunContext[PackContext]) -> dict[str, Any]:
    del run
    return {
        "kinds": {
            "fact": "A durable claim about the person.",
            "episode": "Something that happened in a session.",
            "procedure": "How they like something done.",
            "summary": "A distilled cluster of older notes.",
        },
        "sections": {
            "blocks": "Pinned memory blocks that travel with every turn.",
            "facts": "Highest-ranked memories. Not mixed with account.",
            "account": (
                "Pinned account fields and notes. A separate list; do not treat order "
                "as shared with facts."
            ),
        },
        "scopes": {
            "account": "True in every profile.",
            "profile": "True in this profile only.",
            "session": "True in this conversation only.",
        },
        "trust": {
            "stated": "They said it.",
            "observed": "Lucy saw it happen.",
            "inferred": "Lucy inferred it; they can toggle these off.",
            "untrusted": "Came from a page or a tool. Never retrieved until confirmed.",
        },
    }


__all__ = ["INCOGNITO", "NotesPack"]
