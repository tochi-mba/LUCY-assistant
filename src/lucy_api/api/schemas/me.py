"""The identity document."""

from __future__ import annotations

from pydantic import BaseModel, Field


class MeResponse(BaseModel):
    """The subject of the verified token, and the audience it was minted for."""

    account_id: str = Field(description="Opaque; the only key the hub stores anything under.")
    audience: str
