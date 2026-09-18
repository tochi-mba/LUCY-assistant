"""HTTP helpers for the durable result store, so routers never import weftai."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from weftai.errors import RefResolutionError

from lucy_api.core.errors import LucyError, absent
from lucy_api.sessions.sql_store import session_row
from lucy_api.store.results import SqlResultStore, resolve_stored_ref

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore


def _public(value: object) -> object:
    if isinstance(value, tuple | list):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _public(item) for key, item in value.items()}
    if hasattr(value, "model_dump") and callable(value.model_dump):
        dumped: object = value.model_dump(mode="json")
        return _public(dumped)
    return value


async def list_results(store: SessionStore, account: str, session_id: str) -> list[dict[str, Any]]:
    def read(db: sqlite3.Connection) -> list[dict[str, Any]]:
        session_row(db, account, session_id)
        return [
            {
                "id": item["id"],
                "operation": item["operation"],
                "kind": item["kind"],
                "type": item["type"],
                "count": item["count"],
            }
            for item in SqlResultStore(db).list(session_id)
        ]

    return await store.worker.call(read)


async def get_result(
    store: SessionStore, account: str, session_id: str, result_id: str
) -> dict[str, Any]:
    def read(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        stored = SqlResultStore(db).get(session_id, result_id)
        if stored is None:
            raise absent()
        return {
            "id": stored["id"],
            "operation": stored["operation"],
            "kind": stored["kind"],
            "type": stored["type"],
            "count": stored["count"],
            "data": _public(stored["data"]),
            "notices": list(stored["notices"]),
        }

    return await store.worker.call(read)


async def resolve_result(
    store: SessionStore, account: str, session_id: str, ref: str
) -> dict[str, Any]:
    def read(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session_id)
        try:
            resolved = resolve_stored_ref(SqlResultStore(db), session_id, ref)
        except RefResolutionError as exc:
            code = "bad-ref"
            raise LucyError(code, str(exc), 400) from exc
        if resolved is None:
            raise absent()
        payload = getattr(resolved, "items", resolved)
        return {"ref": ref, "data": _public(payload)}

    return await store.worker.call(read)


__all__ = ["get_result", "list_results", "resolve_result"]
