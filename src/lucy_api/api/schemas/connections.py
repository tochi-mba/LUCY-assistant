"""Public connection metadata; credential material has no representable field."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this annotation at runtime

from pydantic import BaseModel, ConfigDict, Field


class ConnectionResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str = Field(description="The connection identifier.")
    status: str = Field(description="Active, pending, expired or revoked.")
    scopes: list[str] = Field(description="Scopes the provider actually granted.")
    expires_at: datetime | None = Field(description="When this connection expires, if known.")
    last_error: str = Field(description="Why the last refresh failed, without credentials.")


class ConnectionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[ConnectionResource]


class AuthorizationResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connect_url: str = Field(description="A subject-bound link on Lucy's own origin.")
    ticket: str = Field(description="Opaque short-lived authorization handle.")
    expires_at: datetime = Field(description="When this link stops working.")
    poll_url: str = Field(description="Where a client checks whether consent completed.")
    interval: int = Field(default=5, description="Minimum polling interval in seconds.")


class AuthorizationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    profile: str
    status: str
    expires_at: datetime


__all__ = [
    "AuthorizationResource",
    "AuthorizationStatus",
    "ConnectionList",
    "ConnectionResource",
]
