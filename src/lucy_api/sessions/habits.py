"""What a profile has been using lately, so a new conversation starts with it bound.

Which capabilities are bound is decided by recency, and recency was this conversation's
alone, held in memory. So every new conversation started cold: asked to play a song, Lucy
spent a whole round binding music and reading its page before she could look it up -- about
fifteen thousand tokens and fifty seconds on the weakest model -- in every session, for a
person who plays music every day. A restart forgot even the current conversation's.

The steps a profile ran are already recorded, so its habits are read from them: the
capabilities whose operations succeeded in its recent conversations, most recent first.
They only order what is ready; how many are bound is unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.sql_store import SessionStore

HABIT_DAYS = 14
"""How far back a profile's use still says something about what it will want next."""

HABIT_ROWS = 200
"""The most step kinds read, newest first; far more than the handful that get bound."""

DAY_SECONDS = 86_400


async def recent_capabilities(
    store: SessionStore, account: str, profile: str, *, now: float
) -> tuple[str, ...]:
    """The capabilities this profile ran successfully lately, most recently used first.

    An incognito conversation's steps say nothing here: what someone did off the record
    should not shape what the next conversation is ready for.
    """
    since = now - HABIT_DAYS * DAY_SECONDS

    def read(db: sqlite3.Connection) -> list[str]:
        rows = db.execute(
            "SELECT steps.kind, MAX(steps.created_at) AS last FROM steps "
            "JOIN sessions ON sessions.id = steps.session_id "
            "WHERE sessions.account_id=? AND sessions.profile=? AND sessions.incognito=0 "
            "AND steps.status='ok' AND steps.created_at>=? "
            "GROUP BY steps.kind ORDER BY last DESC LIMIT ?",
            (account, profile, since, HABIT_ROWS),
        ).fetchall()
        return [str(row[0]) for row in rows]

    kinds = await store.worker.call(read)
    return tuple(dict.fromkeys(kind.split(".", 1)[0] for kind in kinds if "." in kind))


__all__ = ["HABIT_DAYS", "recent_capabilities"]
