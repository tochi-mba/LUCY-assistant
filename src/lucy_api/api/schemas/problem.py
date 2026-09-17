"""The one error shape, in the dialect RFC 9457 defines.

Every failure the hub produces uses it, so a client -- or a model reading a failed tool
call -- has exactly one document to parse rather than one per route. The family already
speaks this dialect; a hub that answered ``{"detail": ...}`` while its siblings answered
problem documents would make the single most common thing a client does, handling an error,
the one thing it has to special-case per service.

``detail`` names the rule that failed and **never echoes the offending value**. That is not
politeness. A 4xx body from the hub is logged by the caller, shown in a transcript, and
handed back to a model; a value refused for looking like a credential must not be copied
into a log line on its way out.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

PROBLEM_CONTENT_TYPE = "application/problem+json"


class FieldError(BaseModel):
    """One field-level validation failure."""

    location: str = Field(description="Dotted path to the offending field.")
    message: str = Field(description="What is wrong with it.")


class Problem(BaseModel):
    """An error, addressable by type and traceable by request id."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "type": "https://lucy-api.invalid/problems/not-found",
                    "title": "Not found",
                    "status": 404,
                    "detail": "The resource was not found.",
                    "request_id": "5c1f9f0f7f2f4e6c8a1b2c3d4e5f6a7b",
                }
            ]
        }
    )

    type: str = Field(description="A URI identifying the problem kind.")
    title: str = Field(description="Short, human-readable summary of the problem kind.")
    status: int = Field(description="The HTTP status code.")
    detail: str = Field(description="Explanation specific to this occurrence.")
    request_id: str | None = Field(
        default=None,
        description="Correlates this response with the server logs for the same request.",
    )
    errors: list[FieldError] | None = Field(
        default=None,
        description="Per-field detail, present only for validation failures.",
    )
