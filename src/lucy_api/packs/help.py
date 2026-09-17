"""The one pack the model can never lose.

Without ``capabilities.list`` and ``help.operation``, deferred loading is a one-way door:
the model cannot ask for what was held back, and cannot learn a tool it has never seen.
Without ``capabilities.setup``, an unconnected capability is a dead end rather than a
link. Those five operations are therefore always bound, even when every sibling is down.

The handlers read the catalogue off the context rather than closing over it at definition
time. A probe runs *before* the catalogue exists, and an operation defined against a
stale copy would list last turn's connections as this turn's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.operation import define_operation
from weftai.schema.spec import array_schema, integer_schema, object_schema, string_schema
from weftai.schema.types import value

from lucy_api.packs.base import Availability, Permission, SetupPlan, State, connection_required

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from weftai.operation import AnyOperation, RunContext

    from lucy_api.packs.context import PackContext

DOCS_WINDOW = 2_000
"""How many characters ``help.docs`` returns by default.

Small on purpose: the model asked for a topic, not the whole manual, and dumping a
capability's entire markdown into the context is how a help tool becomes the largest
thing in the window.
"""

HELP_MARKDOWN = """# Help

You have capabilities, named the way a person would name them: music, research, notes,
workspace. You never see a service, a port or an HTTP verb.

`capabilities.list` is how you find out what is usable *right now*. An unconnected
capability is absent from your tools; list it, then `capabilities.setup` if the person
wants it.

`help.operation` is how you read one operation in full. Names and one-liners are what you
are bound with; the schema and the examples live here so the preamble stays small.

When a capability was deferred, `capabilities.use` binds it for the rest of this session.
"""


class HelpPack:
    """Always-present operations: list, setup, bind, and the two help reads."""

    id = "help"
    title = "Help"
    summary = "List what you can do, read a capability's docs, and bind one that was deferred."

    @property
    def docs(self) -> Path | None:
        return None

    def permissions(self) -> Sequence[Permission]:
        return ()

    def setup(self) -> SetupPlan | None:
        return None

    async def probe(self, context: PackContext) -> Availability:  # noqa: ARG002 - pack
        return Availability(state=State.ready, detail="always available")

    def operations(self, context: PackContext) -> Sequence[AnyOperation]:  # noqa: ARG002 - pack
        return (
            define_operation(
                {
                    "name": "capabilities.list",
                    "description": (
                        "List every capability, its state, and one line each (capabilities, "
                        "tools, what is connected, music, research, notes, workspace)."
                    ),
                    "input": object_schema({}),
                    "output": value(
                        object_schema(
                            {
                                "capabilities": array_schema(
                                    object_schema(
                                        {
                                            "id": string_schema(),
                                            "title": string_schema(),
                                            "summary": string_schema(),
                                            "state": string_schema(),
                                            "detail": string_schema(),
                                        }
                                    )
                                )
                            }
                        )
                    ),
                    "effects": "read",
                    "run": _list,
                }
            ),
            define_operation(
                {
                    "name": "capabilities.setup",
                    "description": (
                        "How the person connects one capability. Returns a sentence and a "
                        "link; never ask them for a password or a token."
                    ),
                    "input": object_schema({"id": string_schema().describe("The capability id.")}),
                    "output": value(object_schema({"status": string_schema()})),
                    "effects": "read",
                    "run": _setup,
                }
            ),
            define_operation(
                {
                    "name": "capabilities.use",
                    "description": (
                        "Bind a deferred capability for the rest of this session so its "
                        "operations are available on the next turn."
                    ),
                    "input": object_schema({"id": string_schema().describe("The capability id.")}),
                    "output": value(
                        object_schema({"id": string_schema(), "bound": string_schema()})
                    ),
                    "effects": "write",
                    "run": _use,
                }
            ),
            define_operation(
                {
                    "name": "help.docs",
                    "description": (
                        "A capability's authored markdown, windowed. Prefer many small reads "
                        "over one large one (documentation, how to, help)."
                    ),
                    "input": object_schema(
                        {
                            "topic": string_schema().describe("A capability id, or 'help'."),
                            "offset": integer_schema().optional(),
                            "limit": integer_schema().optional(),
                        }
                    ),
                    "output": value(
                        object_schema({"topic": string_schema(), "text": string_schema()})
                    ),
                    "effects": "read",
                    "run": _docs,
                }
            ),
            define_operation(
                {
                    "name": "help.operation",
                    "description": (
                        "One operation's full schema and examples. Use this before calling "
                        "something you have only seen as a name and a one-line description."
                    ),
                    "input": object_schema(
                        {"name": string_schema().describe("The operation name, like music.play.")}
                    ),
                    "output": value(object_schema({"name": string_schema()})),
                    "effects": "read",
                    "run": _operation,
                }
            ),
        )


async def _list(run: RunContext[PackContext]) -> dict[str, Any]:
    catalogue = run.ctx.catalogue
    if catalogue is None:
        return {"capabilities": []}
    return {
        "capabilities": [
            {
                "id": item.pack.id,
                "title": item.pack.title,
                "summary": item.pack.summary,
                "state": item.availability.state.value,
                "detail": item.availability.detail,
                "offer_setup": item.availability.offer_setup,
            }
            for item in catalogue.bound
        ]
    }


async def _setup(run: RunContext[PackContext]) -> dict[str, Any]:
    wanted = str(run.input.get("id", ""))
    bound = _find(run.ctx, wanted)
    if bound is None:
        return {"status": "unknown", "id": wanted, "message": f"there is no capability '{wanted}'"}
    plan = bound.pack.setup()
    if bound.availability.usable:
        return {
            "status": "ready",
            "id": wanted,
            "message": f"{wanted} is already connected.",
        }
    url = bound.availability.connect_url
    summary = plan.summary if plan is not None else bound.availability.detail
    if not bound.availability.offer_setup:
        return {"status": bound.availability.state.value, "id": wanted, "message": summary}
    body = connection_required(
        service=wanted,
        profile=run.ctx.profile,
        connect_url=url,
        scopes=bound.availability.missing_scopes,
    )
    if summary:
        body["message"] = summary
    return body


async def _use(run: RunContext[PackContext]) -> dict[str, Any]:
    wanted = str(run.input.get("id", ""))
    bound = _find(run.ctx, wanted)
    if bound is None:
        return {"bound": False, "id": wanted, "message": f"there is no capability '{wanted}'"}
    if not bound.availability.usable:
        return {"bound": False, "id": wanted, "message": f"{wanted} is not usable this turn"}
    run.ctx.bound_ids.add(wanted)
    return {"bound": True, "id": wanted, "message": f"{wanted} will be available next turn"}


async def _docs(run: RunContext[PackContext]) -> dict[str, Any]:
    topic = str(run.input.get("topic", "help"))
    offset = int(run.input.get("offset") or 0)
    limit = int(run.input.get("limit") or DOCS_WINDOW)
    text = HELP_MARKDOWN if topic == "help" else _pack_docs(run.ctx, topic)
    window = text[max(offset, 0) : max(offset, 0) + max(limit, 0)]
    return {
        "topic": topic,
        "text": window,
        "offset": max(offset, 0),
        "limit": max(limit, 0),
        "total": len(text),
        "showing": f"showing {len(window)} of {len(text)} characters",
    }


async def _operation(run: RunContext[PackContext]) -> dict[str, Any]:
    name = str(run.input.get("name", ""))
    for item in run.ctx.catalogue.bound if run.ctx.catalogue is not None else ():
        for operation in item.operations:
            if operation.name == name:
                examples = [
                    {"input": example.input, "note": example.note} for example in operation.examples
                ]
                return {
                    "name": operation.name,
                    "description": operation.description,
                    "effects": operation.effects,
                    "examples": examples,
                }
    return {"name": name, "error": f"no operation named '{name}' in this turn's catalogue"}


def _find(context: PackContext, pack_id: str) -> Any:
    if context.catalogue is None:
        return None
    return context.catalogue.get(pack_id)


def _pack_docs(context: PackContext, topic: str) -> str:
    bound = _find(context, topic)
    if bound is None:
        return f"there is no capability '{topic}'"
    path = bound.pack.docs
    if path is None:
        summary: str = bound.pack.summary
        return summary
    text: str = path.read_text(encoding="utf-8")
    return text


__all__ = ["DOCS_WINDOW", "HELP_MARKDOWN", "HelpPack"]
