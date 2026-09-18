"""Uploaded files and session artifacts, without a disk path on the wire."""

from __future__ import annotations

from pydantic import BaseModel

from lucy_api.api.schemas.sessions import Page


class FileResource(BaseModel):
    """A file this person uploaded. The disk path is not a public field."""

    id: str
    filename: str
    bytes: int
    mime_type: str
    purpose: str
    created_at: float


class ArtifactResource(BaseModel):
    """Something a session produced. It dies with the session."""

    id: str
    session_id: str
    bytes: int
    mime_type: str
    produced_by: str
    created_at: float


FilePage = Page[FileResource]
ArtifactPage = Page[ArtifactResource]

__all__ = [
    "ArtifactPage",
    "ArtifactResource",
    "FilePage",
    "FileResource",
]
