"""Webhook destinations. The secret is a create-once field, never a list field."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CreateWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=8,
        max_length=2048,
        description="HTTPS destination. Lucy posts a signal, never a transcript.",
        examples=["https://example.com/lucy/hook"],
    )


class WebhookResource(BaseModel):
    """A registered destination. `secret` is present only on the create response."""

    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    created_at: float
    secret: str | None = None


class WebhookPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[WebhookResource]
