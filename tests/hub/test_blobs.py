"""Filename sanitisation and the size ceiling, without HTTP."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import ACCOUNT

from lucy_api.blobs import Blobs
from lucy_api.blobs.store import MAX_BYTES, _part, safe_filename, too_large
from lucy_api.core.config import Settings
from lucy_api.core.container import _blobs_root
from lucy_api.core.errors import LucyError
from lucy_api.sessions.models import CreateSession, Cursor

if TYPE_CHECKING:
    from lucy_api.sessions.sql_store import SessionStore


def test_a_path_in_a_filename_is_reduced_to_its_final_segment() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\\\Windows\\\\a.txt") == "a.txt"
    assert safe_filename("   ") == "upload"


def test_an_unsafe_id_is_a_miss_rather_than_a_path() -> None:
    with pytest.raises(LucyError) as raised:
        _part("../secret")
    assert raised.value.status == 404


def test_an_oversize_upload_is_named_as_too_large() -> None:
    error = too_large()
    assert error.status == 413
    assert "16 MiB" in str(error)
    assert len(b"x") <= MAX_BYTES


def test_blobs_sit_beside_a_file_database(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_path=str(tmp_path / "lucy.sqlite3"))
    assert _blobs_root(settings) == tmp_path / "blobs"


def test_an_explicit_blobs_path_wins(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, database_path=":memory:", blobs_path=str(tmp_path / "keep"))
    assert _blobs_root(settings) == tmp_path / "keep"


def test_an_in_memory_database_does_not_invent_a_blobs_directory() -> None:
    settings = Settings(_env_file=None, database_path=":memory:")
    assert _blobs_root(settings) is None


async def test_a_configured_root_is_left_on_disk_when_the_store_closes(
    sessions_store: SessionStore, tmp_path: Path
) -> None:
    root = tmp_path / "keep"
    blobs = Blobs(sessions_store, root=root)
    blobs.close()
    blobs.close()
    assert root.is_dir()


async def test_blank_metadata_falls_back_to_safe_defaults(sessions_store: SessionStore) -> None:
    blobs = Blobs(sessions_store)
    try:
        session = await sessions_store.create(ACCOUNT, CreateSession(), "blob-session")
        uploaded = await blobs.upload(
            ACCOUNT,
            filename="a.txt",
            data=b"x",
            mime_type="  ",
            purpose="  ",
            key="blank-meta",
        )
        assert uploaded["mime_type"] == "application/octet-stream"
        assert uploaded["purpose"] == "upload"
        artifact = await blobs.put_artifact(
            ACCOUNT,
            str(session["id"]),
            name="out.txt",
            data=b"y",
            mime_type=" ",
            produced_by=" ",
        )
        assert artifact["mime_type"] == "application/octet-stream"
        assert artifact["produced_by"] == "workspace"
        listed = await blobs.list(ACCOUNT, Cursor(limit=1, after=str(uploaded["id"])))
        assert listed["data"] == []
        artifacts = await blobs.list_artifacts(ACCOUNT, str(session["id"]), Cursor(order="desc"))
        assert artifacts["data"][0]["id"] == artifact["id"]
        _, body = await blobs.bytes_for_artifact(ACCOUNT, str(artifact["id"]))
        assert body == b"y"
        owned = blobs.root
        blobs.close()
        blobs.close()
        assert not owned.exists()
    finally:
        blobs.close()
