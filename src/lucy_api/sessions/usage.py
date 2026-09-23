"""Session usage as a read of the row, not a second ledger.

The session already sums tokens as turns finish. A separate counter that can drift from
those columns is how a usage page starts lying. This module only reads what the turn loop
already wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lucy_api.sessions.sql_store import session_row

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore


async def session_usage(store: SessionStore, account: str, session_id: str) -> dict[str, Any]:
    def read(db: sqlite3.Connection) -> dict[str, Any]:
        row = session_row(db, account, session_id)
        turns = db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens),0) AS input_tokens, "
            "COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
            "COALESCE(SUM(iterations),0) AS iterations, "
            "COALESCE(SUM(cost_micros),0) AS cost_micros FROM turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return {
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cost_micros": int(row["cost_micros"]),
            "turns": int(turns["n"]),
            "turn_input_tokens": int(turns["input_tokens"]),
            "turn_output_tokens": int(turns["output_tokens"]),
            "turn_cost_micros": int(turns["cost_micros"]),
            "turn_cache_read_tokens": int(turns["cache_read_tokens"]),
            "turn_iterations": int(turns["iterations"]),
        }

    return await store.worker.call(read)


__all__ = ["session_usage"]
