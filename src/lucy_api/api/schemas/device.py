"""OAuth device-flow wire models; secret values are write-only or returned once."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeviceCodeResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_code: str = Field(description="High-entropy code used only by the requesting client.")
    user_code: str = Field(description="Short code shown to the person authorizing the client.")
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class DeviceTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_code: str = Field(min_length=16, max_length=256)


class DeviceTokenResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type, not a credential


class DeviceDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_code: str = Field(min_length=4, max_length=32)
    approve: bool = True


__all__ = [
    "DeviceCodeResource",
    "DeviceDecisionRequest",
    "DeviceTokenRequest",
    "DeviceTokenResource",
]
