"""The version-one setup discovery contract.

Deployment readiness and a person's connection are independent facts. A successful
readiness probe must never become a claim that the person has connected their account.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Readiness = Literal["ready", "degraded", "unavailable"]
ConnectionState = Literal["unknown", "not_required"]
CheckState = Literal["ready", "degraded", "unknown"]


class SetupAction(BaseModel):
    """A documented next step, never an invented authorization URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["documentation", "operator"]
    label: str
    description: str
    url: str | None = None


class SetupCheck(BaseModel):
    """An allowlisted dependency name and its reported health, without raw details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    state: CheckState


class SetupService(BaseModel):
    """One optional capability or required identity dependency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    required: bool
    state: Readiness
    connection_state: ConnectionState
    summary: str
    checks: list[SetupCheck] = Field(default_factory=list)
    actions: list[SetupAction]


class SetupResponse(BaseModel):
    """The authenticated account and its setup catalogue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str
    services: list[SetupService]
