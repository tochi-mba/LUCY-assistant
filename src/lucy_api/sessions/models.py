"""Validated input vocabulary, independent of HTTP and model SDKs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

InputPolicy = Literal["reject", "enqueue", "interrupt", "rollback"]
PermissionMode = Literal["ask", "accept_edits", "plan", "auto"]
TurnStatus = Literal[
    "queued", "running", "input_required", "auth_required", "completed", "failed", "cancelled"
]
TERMINAL = frozenset({"completed", "failed", "cancelled"})


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


class UpdateSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    input_policy: InputPolicy | None = None
    permission_mode: PermissionMode | None = None
    archived: bool | None = None


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
