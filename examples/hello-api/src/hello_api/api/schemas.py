"""Response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    status: str = Field(description="Always 'alive'.")
    version: str
    environment: str
    uptime_seconds: float


class CheckResult(BaseModel):
    status: str
    detail: dict[str, object] = Field(default_factory=dict)


class ReadyResponse(BaseModel):
    status: str
    version: str
    environment: str
    uptime_seconds: float
    checks: dict[str, CheckResult]


class WhoAmIResponse(BaseModel):
    account_id: str
    audience: str
