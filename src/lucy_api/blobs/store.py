"""Disk plus the `files` and `artifacts` tables, with the path always ours.

Site-supplied and person-supplied names are hostile. The id is the filename on disk; the
original name is metadata for Content-Disposition. Building a path from either is how a
`../` in an upload becomes a write outside the store.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError, absent
from lucy_api.sessions.sql_store import (
    IdempotentWrite,
    audit_row,
    identifier,
    page,
    row_value,
    session_row,
)

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.models import Cursor
    from lucy_api.sessions.sql_store import SessionStore

MAX_BYTES = 16 * 1024 * 1024
"""Sixteen mebibytes. Larger uploads belong in the workspace, not in this store."""

TOO_LARGE = "This upload is larger than 16 MiB; put it in the workspace instead."
ACCOUNT_PART = re.compile(r"^[A-Za-z0-9_-]+$")


def safe_filename(name: str) -> str:
    """A single path segment with nothing a filesystem or a header would treat as structure."""
    base = Path(name.replace("\\", "/")).name
    cleaned = "".join(
        char if char.isprintable() and char not in '<>:"|?*' else "_" for char in base
    )
    cleaned = cleaned.strip(" .")[:255]
    return cleaned or "upload"


def too_large() -> LucyError:
    return LucyError("payload-too-large", TOO_LARGE, 413)


class Blobs:
    """Account files and session artifacts, keyed so a foreign id is indistinguishable from none."""

    def __init__(self, store: SessionStore, root: Path | None = None) -> None:
        self._store = store
        self._owned: tempfile.TemporaryDirectory[str] | None = None
        if root is None:
            self._owned = tempfile.TemporaryDirectory(prefix="lucy-blobs-")
            root = Path(self._owned.name)
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Drop a process-owned temp tree. A configured root is the operator's to keep."""
        if self._owned is not None:
            self._owned.cleanup()
            self._owned = None

    async def upload(  # noqa: PLR0913 - filename, bytes, type, purpose and idempotency key
        self,
        account: str,
        *,
        filename: str,
        data: bytes,
        mime_type: str,
        purpose: str,
        key: str,
    ) -> dict[str, Any]:
        if len(data) > MAX_BYTES:
            raise too_large()
        name = safe_filename(filename)
        mime = mime_type.strip() or "application/octet-stream"
        reason = purpose.strip() or "upload"
        digest = hashlib.sha256(data).hexdigest()
        write = IdempotentWrite(
            account,
            "POST /v1/files",
            key,
            body={"filename": name, "purpose": reason, "sha256": digest, "bytes": len(data)},
            status=201,
        )

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            file_id = identifier("fil")
            path = self._file_path(account, file_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            db.execute(
                "INSERT INTO files(id,account_id,filename,bytes,mime_type,purpose,path,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    file_id,
                    account,
                    name,
                    len(data),
                    mime,
                    reason,
                    str(path),
                    time.time(),
                ),
            )
            path.write_bytes(data)
            return _public_file(_owned_file(db, account, file_id))

        return await self._store.idempotent(write, apply)

    async def get(self, account: str, file_id: str) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            return _public_file(_owned_file(db, account, file_id))

        return await self._store.worker.call(read)

    async def list(self, account: str, selection: Cursor) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            rows = db.execute(
                "SELECT * FROM files WHERE account_id=? ORDER BY created_at,id", (account,)
            ).fetchall()
            return page(
                [_public_file(row_value(row)) for row in rows],
                selection.limit,
                selection.after,
                selection.before,
                selection.order,
            )

        return await self._store.worker.call(read)

    async def delete(self, account: str, file_id: str) -> None:
        def apply(db: sqlite3.Connection) -> None:
            row = _owned_file(db, account, file_id)
            db.execute("DELETE FROM files WHERE id=? AND account_id=?", (file_id, account))
            Path(str(row["path"])).unlink(missing_ok=True)

        await self._store.transaction(apply)

    async def put_artifact(  # noqa: PLR0913 - session, name, bytes, type, producer
        self,
        account: str,
        session: str,
        *,
        name: str,
        data: bytes,
        mime_type: str,
        produced_by: str,
    ) -> dict[str, Any]:
        if len(data) > MAX_BYTES:
            raise too_large()
        mime = mime_type.strip() or "application/octet-stream"
        produced = produced_by.strip() or "workspace"

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            session_row(db, account, session)
            artifact_id = identifier("art")
            path = self._artifact_path(session, artifact_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            db.execute(
                "INSERT INTO artifacts(id,session_id,path,bytes,mime_type,produced_by,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (artifact_id, session, str(path), len(data), mime, produced, time.time()),
            )
            path.write_bytes(data)
            return _public_artifact(_owned_artifact(db, account, artifact_id), name=name)

        return await self._store.transaction(apply)

    async def get_artifact(self, account: str, artifact_id: str) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            return _public_artifact(_owned_artifact(db, account, artifact_id))

        return await self._store.worker.call(read)

    async def list_artifacts(self, account: str, session: str, selection: Cursor) -> dict[str, Any]:
        def read(db: sqlite3.Connection) -> dict[str, Any]:
            session_row(db, account, session)
            rows = db.execute(
                "SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at,id", (session,)
            ).fetchall()
            return page(
                [_public_artifact(row_value(row)) for row in rows],
                selection.limit,
                selection.after,
                selection.before,
                selection.order,
            )

        return await self._store.worker.call(read)

    async def bytes_for_file(self, account: str, file_id: str) -> tuple[dict[str, Any], bytes]:
        def read(db: sqlite3.Connection) -> tuple[dict[str, Any], bytes]:
            row = _public_file(_owned_file(db, account, file_id))
            return row, _read_path(str(row["path"]))

        return await self._store.worker.call(read)

    async def bytes_for_artifact(
        self, account: str, artifact_id: str
    ) -> tuple[dict[str, Any], bytes]:
        def read(db: sqlite3.Connection) -> tuple[dict[str, Any], bytes]:
            row = _public_artifact(_owned_artifact(db, account, artifact_id))
            return row, _read_path(str(row["path"]))

        return await self._store.worker.call(read)

    async def delete_session(self, account: str, session: str) -> None:
        def apply(db: sqlite3.Connection) -> None:
            session_row(db, account, session)
            rows = db.execute(
                "SELECT path FROM artifacts WHERE session_id=?", (session,)
            ).fetchall()
            for row in rows:
                Path(str(row["path"])).unlink(missing_ok=True)

        await self._store.worker.call(apply)

    async def erase_account(self, account: str) -> tuple[tuple[str, str], ...]:
        def apply(
            db: sqlite3.Connection,
        ) -> tuple[tuple[str, str], ...]:
            file_paths = [
                str(row["path"])
                for row in db.execute(
                    "SELECT path FROM files WHERE account_id=?", (account,)
                ).fetchall()
            ]
            artifact_paths = [
                str(row["path"])
                for row in db.execute(
                    "SELECT artifacts.path FROM artifacts "
                    "JOIN sessions ON sessions.id=artifacts.session_id "
                    "WHERE sessions.account_id=?",
                    (account,),
                ).fetchall()
            ]
            workspaces = tuple(
                (str(row["workspace_environment_id"]), str(row["workspace_rel"]))
                for row in db.execute(
                    "SELECT workspace_environment_id, workspace_rel FROM sessions "
                    "WHERE account_id=? AND workspace_environment_id IS NOT NULL "
                    "AND workspace_rel IS NOT NULL AND workspace_rel != ''",
                    (account,),
                ).fetchall()
            )
            for path in file_paths + artifact_paths:
                Path(path).unlink(missing_ok=True)
            audit_row(db, account, "account.erased", detail={"workspaces": len(workspaces)})
            db.execute("DELETE FROM sessions WHERE account_id=?", (account,))
            db.execute("DELETE FROM files WHERE account_id=?", (account,))
            db.execute("DELETE FROM mcp_servers WHERE account_id=?", (account,))
            db.execute("DELETE FROM permission_grants WHERE account_id=?", (account,))
            db.execute("DELETE FROM device_codes WHERE account_id=?", (account,))
            db.execute("DELETE FROM webhooks WHERE account_id=?", (account,))
            return workspaces

        return await self._store.transaction(apply)

    def _file_path(self, account: str, file_id: str) -> Path:
        return self._contained("files", _part(account), _part(file_id))

    def _artifact_path(self, session: str, artifact_id: str) -> Path:
        return self._contained("artifacts", _part(session), _part(artifact_id))

    def _contained(self, *parts: str) -> Path:
        return (self.root.joinpath(*parts)).resolve()


def _part(value: str) -> str:
    if not ACCOUNT_PART.fullmatch(value):
        raise absent()
    return value


def _owned_file(db: sqlite3.Connection, account: str, file_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM files WHERE id=? AND account_id=?", (file_id, account)
    ).fetchone()
    if row is None:
        raise absent()
    return row_value(row)


def _owned_artifact(db: sqlite3.Connection, account: str, artifact_id: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT artifacts.* FROM artifacts "
        "JOIN sessions ON sessions.id=artifacts.session_id "
        "WHERE artifacts.id=? AND sessions.account_id=?",
        (artifact_id, account),
    ).fetchone()
    if row is None:
        raise absent()
    return row_value(row)


def _public_file(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "bytes": row["bytes"],
        "mime_type": row["mime_type"],
        "purpose": row["purpose"],
        "created_at": row["created_at"],
        "path": row["path"],
    }


def _public_artifact(row: dict[str, Any], *, name: str = "") -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "bytes": row["bytes"],
        "mime_type": row["mime_type"],
        "produced_by": row["produced_by"],
        "created_at": row["created_at"],
        "path": row["path"],
        "filename": safe_filename(name) if name else Path(str(row["path"])).name,
    }


def _read_path(path: str) -> bytes:
    target = Path(path)
    if not target.is_file():
        raise absent()
    return target.read_bytes()
