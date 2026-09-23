"""Validated input vocabulary, independent of HTTP and model SDKs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

InputPolicy = Literal["reject", "enqueue", "interrupt", "rollback"]
PermissionMode = Literal["ask", "accept_edits", "plan", "auto"]
TurnStatus = Literal[
    "queued", "running", "input_required", "auth_required", "completed", "failed", "cancelled"
]
TERMINAL = frozenset({"completed", "failed", "cancelled"})


CapabilityNames = list[Annotated[str, Field(min_length=1, max_length=64)]]
"""Capability ids, as a person names them: `agents`, `music`. Never a service."""

Apply = Literal["now", "after_turn"]
"""The answer to the warning a change gets while a turn is running: apply it to the
running turn, or hold it until that turn ends. Absent, the warning is the answer."""

BEHAVIOUR = frozenset({"input_policy", "permission_mode", "disabled_capabilities"})
"""The session fields that change what a running turn may do. A title does not."""


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str = Field(default="personal", min_length=1, max_length=128)
    title: str = Field(default="New conversation", max_length=200)
    model: str = Field(default="openai:gpt-5", min_length=1, max_length=128)
    thinking_config: str = Field(default="default", max_length=128)
    persona: str = Field(default="default", max_length=128)
    input_policy: InputPolicy = "enqueue"
    permission_mode: PermissionMode = "ask"
    incognito: bool = False
    disabled_capabilities: CapabilityNames = Field(default_factory=list, max_length=32)


class UpdateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    input_policy: InputPolicy | None = None
    permission_mode: PermissionMode | None = None
    disabled_capabilities: CapabilityNames | None = Field(default=None, max_length=32)
    archived: bool | None = None
    apply: Apply | None = None


class InputEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "input.message",
        "input.tool_result",
        "input.approval",
        "input.elicitation_response",
        "input.cancel",
    ]
    content: JsonValue = None
    turn_id: str | None = None
    approval_id: str | None = None
    approved: bool | None = None
    instruction: str | None = Field(default=None, max_length=4096)
    lifetime: Literal["once", "session", "profile", "account"] = "once"
    call_id: str | None = None


class InputBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[InputEvent] = Field(min_length=1, max_length=100)


class ForkSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str | None = None


DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class Cursor(BaseModel):
    """Where to read from and how much, for every collection the hub exposes.

    Cursor-only, never a page number: an append-only log grows while it is being read, and
    offset paging over a growing collection both duplicates rows and skips them.

    Extra fields are refused so a misspelled `ordr=desc` is an error rather than a filter
    that silently did not apply -- which on a transcript is the difference between "this is
    the conversation" and "this is the part a typo let through".
    """

    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    order: Literal["asc", "desc"] = "asc"
    after: str | None = Field(default=None, description="Start after this id.")
    before: str | None = Field(default=None, description="Stop before this id.")


@dataclass(frozen=True, slots=True)
class Outcome:
    """How a turn ended.

    The hub's reason and the provider's are separate fields because they answer different
    questions and a client has to show them differently: `error_max_iterations` can be
    resumed and a `refusal` cannot. Grouping them here rather than passing three parallel
    arguments keeps a call site from reading `close_turn(store, a, t, "failed", None, None)`,
    where the reader has to count commas to find out what the Nones were.
    """

    status: str
    termination: str | None = None
    stop_reason: str | None = None
