"""What the two health routes return.

They are different documents because they answer different questions. Liveness says the
process is running and nothing else; readiness says which dependencies are usable and
answers 503 when one is not. An orchestrator restarts a container whose liveness check
fails, and restarting a process does not fix the service it depends on.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class LivenessResponse(BaseModel):
    """No I/O went into this answer, and it never fails."""

    status: str = Field(description="Always 'alive'.")
    version: str
    environment: str
    uptime_seconds: float


class CheckResult(BaseModel):
    """One dependency's verdict. ``detail`` carries counts and yes/no, never a name."""

    status: str
    detail: dict[str, object] = Field(default_factory=dict)


class ReadyResponse(BaseModel):
    """Every dependency, and the worst of them as the overall status."""

    status: str
    version: str
    environment: str
    uptime_seconds: float
    checks: dict[str, CheckResult]
