"""The small MCP tool surface: Lucy's session tools plus weftai's three unpatched names.

weftai's ``run_plan``, ``describe_operations`` and ``get_result`` descriptions are part
of the npm parity contract. They are copied or generated the same way weftai's own MCP
server does; they are never rewritten to mention Lucy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lucy_api.mcp.protocol import GONE, TOOLS_TTL_MS

# Byte-identical to weftai.mcp.create_mcp_server. A test pins them against that module
# so a weftai bump that changes the contract fails here rather than silently drifting.
WEFTAI_DESCRIBE_OPERATIONS = (
    "List operations this server can run, with input shapes and the $ref syntax."
)
WEFTAI_GET_RESULT = (
    "Read a stored result by $ref (for example $owned or $owned[2]). Positions index the "
    "full set, not a preview."
)

SESSION_ID = {
    "type": "string",
    "description": (
        "Opaque id returned by lucy_session_create. State is keyed to the verified "
        "caller; a handle from somebody else looks expired."
    ),
}


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    return schema


def _read() -> dict[str, bool]:
    return {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}


def _write(*, open_world: bool) -> dict[str, bool]:
    return {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": open_world}


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, bool]


def lucy_tools() -> tuple[Tool, ...]:
    """Lucy's own tools. weftai's three are appended at list time so their descriptions
    can include the operations actually bound for this person."""
    return (
        Tool(
            name="lucy_session_create",
            description=(
                "Start a Lucy conversation and attach an isolated workspace. Returns an "
                "opaque session_id in structuredContent. Every other Lucy tool takes that "
                "id. A handle lasts as long as the conversation; once it is gone the error "
                f"is '{GONE}' so the model creates a new one instead of retrying."
            ),
            input_schema=_object(
                {
                    "title": {"type": "string", "description": "Optional conversation title."},
                    "profile": {"type": "string", "description": "Keyring profile name."},
                }
            ),
            annotations=_write(open_world=False),
        ),
        Tool(
            name="lucy_chat",
            description=(
                "Send a person's message into a conversation. Returns the queued turn id. "
                "Closing the MCP client does not cancel the turn."
            ),
            input_schema=_object(
                {
                    "session_id": SESSION_ID,
                    "text": {"type": "string", "description": "What the person said."},
                },
                ("session_id", "text"),
            ),
            annotations=_write(open_world=True),
        ),
        Tool(
            name="lucy_list_capabilities",
            description=(
                "Every installed capability, whether it is usable for this person, and a "
                "sentence saying why not. Unconnected capabilities stay listed."
            ),
            input_schema=_object(
                {"profile": {"type": "string", "description": "Keyring profile name."}}
            ),
            annotations=_read(),
        ),
        Tool(
            name="lucy_connect",
            description=(
                "Start connecting a capability in the browser. Pass the capability id from "
                "lucy_list_capabilities (for example music), never a service name. Returns "
                "a Lucy-origin URL the person opens."
            ),
            input_schema=_object(
                {
                    "capability": {
                        "type": "string",
                        "description": "Capability id, for example music.",
                    },
                    "profile": {"type": "string"},
                },
                ("capability",),
            ),
            annotations=_write(open_world=True),
        ),
        Tool(
            name="lucy_get_session_items",
            description="Read the transcript of one conversation, oldest first.",
            input_schema=_object({"session_id": SESSION_ID}, ("session_id",)),
            annotations=_read(),
        ),
        Tool(
            name="lucy_run_plan",
            description=(
                "Run a weftai plan against the capabilities bound for this conversation. "
                "Same engine as run_plan; takes an explicit session_id."
            ),
            input_schema=_object(
                {
                    "session_id": SESSION_ID,
                    "steps": {"type": "array", "description": "weftai plan steps."},
                },
                ("session_id", "steps"),
            ),
            annotations=_write(open_world=True),
        ),
        Tool(
            name="lucy_get_result",
            description="Read one stored tool result by the id Lucy returned when it was saved.",
            input_schema=_object(
                {
                    "session_id": SESSION_ID,
                    "result_id": {"type": "string"},
                },
                ("session_id", "result_id"),
            ),
            annotations=_read(),
        ),
    )


def weftai_tools(run_plan_description: str) -> tuple[Tool, ...]:
    """weftai's three tools. ``run_plan``'s description is ``Registry.describe()``."""
    return (
        Tool(
            name="run_plan",
            description=run_plan_description,
            input_schema=_object(
                {
                    "session_id": SESSION_ID,
                    "steps": {"type": "array", "description": "weftai plan steps."},
                },
                ("session_id", "steps"),
            ),
            annotations=_write(open_world=True),
        ),
        Tool(
            name="describe_operations",
            description=WEFTAI_DESCRIBE_OPERATIONS,
            input_schema=_object({"session_id": SESSION_ID}),
            annotations=_read(),
        ),
        Tool(
            name="get_result",
            description=WEFTAI_GET_RESULT,
            input_schema=_object(
                {
                    "session_id": SESSION_ID,
                    "ref": {
                        "type": "string",
                        "description": "A $stepId or $stepId[1,3] reference.",
                    },
                },
                ("session_id", "ref"),
            ),
            annotations=_read(),
        ),
    )


def as_list_entry(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": tool.annotations,
    }


def listed(run_plan_description: str) -> list[dict[str, Any]]:
    """Deterministic order so prompt caches survive identical catalogues."""
    tools = [*lucy_tools(), *weftai_tools(run_plan_description)]
    return [as_list_entry(tool) for tool in sorted(tools, key=lambda item: item.name)]


def cache_hint() -> dict[str, Any]:
    return {"ttlMs": TOOLS_TTL_MS, "cacheScope": "private"}
