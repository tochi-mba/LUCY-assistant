"""Files a person keeps, and artifacts a session produced.

A file belongs to an account. An artifact belongs to a session. A stranger's id is a 404
in both cases, because a 403 would confirm the resource exists.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Path, UploadFile, status
from fastapi.responses import Response

from lucy_api.api.dependencies import ActingAsDep, ContainerDep, IdempotencyKeyDep
from lucy_api.api.schemas.files import ArtifactResource, FilePage, FileResource
from lucy_api.api.schemas.problem import Problem
from lucy_api.api.schemas.sessions import SelectionDep

router = APIRouter(prefix="/v1", tags=["files"])

_PROBLEM: dict[str, Any] = {"model": Problem}
_ADDRESSED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: _PROBLEM,
    status.HTTP_404_NOT_FOUND: _PROBLEM,
    status.HTTP_413_CONTENT_TOO_LARGE: _PROBLEM,
    status.HTTP_422_UNPROCESSABLE_CONTENT: _PROBLEM,
}
_IDEMPOTENT = {
    **_ADDRESSED,
    status.HTTP_409_CONFLICT: _PROBLEM,
}
FileId = Annotated[str, Path(min_length=1, max_length=64)]
ArtifactId = Annotated[str, Path(min_length=1, max_length=64)]


@router.post(
    "/files",
    status_code=status.HTTP_201_CREATED,
    operation_id="upload_file",
    summary="Keep a file with this account",
    response_model=FileResource,
    responses=_IDEMPOTENT,
    description=(
        "Stores one file against the verified account. The original name is metadata for "
        "download headers; the bytes live under an id Lucy generated. `Idempotency-Key` is "
        "required: retrying the same body returns the original file."
    ),
)
async def upload_file(
    acting: ActingAsDep,
    container: ContainerDep,
    idempotency_key: IdempotencyKeyDep,
    file: Annotated[UploadFile, File()],
    purpose: Annotated[str, Form()] = "upload",
) -> FileResource:
    data = await file.read()
    name = file.filename or "upload"
    mime = file.content_type or "application/octet-stream"
    row = await container.blobs.upload(
        acting.account_id,
        filename=name,
        data=data,
        mime_type=mime,
        purpose=purpose,
        key=idempotency_key,
    )
    return FileResource.model_validate(row)


@router.get(
    "/files",
    operation_id="list_files",
    summary="Files this account has uploaded",
    response_model=FilePage,
    responses=_ADDRESSED,
    description="Cursor-paged. A file outlives the session that uploaded it.",
)
async def list_files(
    acting: ActingAsDep, container: ContainerDep, selection: SelectionDep
) -> FilePage:
    page = await container.blobs.list(acting.account_id, selection)
    return FilePage.model_validate(page)


@router.get(
    "/files/{file_id}",
    operation_id="get_file",
    summary="One uploaded file's metadata",
    response_model=FileResource,
    responses=_ADDRESSED,
)
async def get_file(file_id: FileId, acting: ActingAsDep, container: ContainerDep) -> FileResource:
    row = await container.blobs.get(acting.account_id, file_id)
    return FileResource.model_validate(row)


@router.get(
    "/files/{file_id}/content",
    operation_id="get_file_content",
    summary="The bytes of one uploaded file",
    responses=_ADDRESSED,
    description="Content-Disposition uses the sanitized original name. A miss is 404.",
)
async def get_file_content(
    file_id: FileId, acting: ActingAsDep, container: ContainerDep
) -> Response:
    row, body = await container.blobs.bytes_for_file(acting.account_id, file_id)
    return _bytes(body, str(row["mime_type"]), str(row["filename"]))


@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_file",
    summary="Forget one uploaded file",
    responses=_ADDRESSED,
)
async def delete_file(file_id: FileId, acting: ActingAsDep, container: ContainerDep) -> Response:
    await container.blobs.delete(acting.account_id, file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/artifacts/{artifact_id}",
    operation_id="get_artifact",
    summary="One artifact a session produced",
    response_model=ArtifactResource,
    responses=_ADDRESSED,
)
async def get_artifact(
    artifact_id: ArtifactId, acting: ActingAsDep, container: ContainerDep
) -> ArtifactResource:
    row = await container.blobs.get_artifact(acting.account_id, artifact_id)
    return ArtifactResource.model_validate(row)


@router.get(
    "/artifacts/{artifact_id}/content",
    operation_id="get_artifact_content",
    summary="The bytes of one artifact",
    responses=_ADDRESSED,
)
async def get_artifact_content(
    artifact_id: ArtifactId, acting: ActingAsDep, container: ContainerDep
) -> Response:
    row, body = await container.blobs.bytes_for_artifact(acting.account_id, artifact_id)
    name = str(row.get("filename") or row["id"])
    return _bytes(body, str(row["mime_type"]), name)


def _bytes(body: bytes, mime: str, filename: str) -> Response:
    quoted = filename.replace("\\", "_").replace('"', "_")
    return Response(
        content=body,
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{quoted}"'},
    )
