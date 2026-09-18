"""Public MCP server registry; tool listings are data, never credentials."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class RegisterMcpServer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]*$",
        description="Stable id used as the mcp.<name> capability namespace.",
        examples=["docs"],
    )
    url: HttpUrl = Field(description="HTTPS URL Lucy will POST tools/list to.")


class McpServerResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    url: str
    state: str = Field(description="ready, unreachable, or pin_mismatch.")
    tools: list[dict[str, object]]
    digest: str = Field(description="SHA-256 of the pinned listing. A change is a rug pull.")
    last_seen: float | None
    created_at: float


class McpServerList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[McpServerResource]
