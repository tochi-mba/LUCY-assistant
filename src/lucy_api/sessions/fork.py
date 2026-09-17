"""Forking a session: copy the transcript, renumber it, and detach the workspace.

Two decisions here are the whole module.

**Ids are remapped, not copied.** Every item gets a new id and its ``parent_id`` is rewritten
to the new id of whatever its parent became. A byte copy would be shorter and would leave
every item in the fork pointing at an item in the session it came from, so deleting the
original would strand the copy and reading the copy would walk into somebody else's
transcript. A parent that was not copied -- which is what the cut point means for everything
after it -- becomes no parent at all rather than a dangling reference.

**The workspace is detached.** A fork starts without an environment or directory.
Sharing or copying files requires an explicit later workspace action, so branching a
conversation never silently gives a second writer access to the original files.

Turns and agents are deliberately left behind. A turn belongs to the run that produced it,
and copying one would either double-count what it spent or leave the fork's items pointing at
a turn in another session's ledger -- the dangling reference this module exists to avoid. So
a copied item keeps its content and loses its ``turn_id``: the fork has no history of
*running*, only of what was said.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from lucy_api import __version__
from lucy_api.core.errors import absent
from lucy_api.sessions.sql_store import event_row, identifier, row_value, session_row

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from lucy_api.sessions.sql_store import SessionStore

IDLE = "idle"
DURABLE = "durable"

_COPY_ITEM = (
    "INSERT INTO items (id,session_id,seq,parent_id,turn_id,agent_id,type,role,"
    "content_json,tokens,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
)
_NEW_SESSION = """INSERT INTO sessions
 (id,account_id,profile,title,status,model,thinking_config,persona,parent_session_id,
  forked_from_item,workspace_environment_id,workspace_rel,harness_version,input_policy,
  durability_mode,permission_mode,incognito,created_at,updated_at)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _prefix(rows: Sequence[sqlite3.Row], item_id: str | None) -> list[sqlite3.Row]:
    """The items the fork inherits: everything, or everything up to and including ``item_id``.

    An item id that is not in this session is refused rather than ignored. Ignoring it would
    silently fork the whole conversation when somebody asked to fork the first half, and the
    two results are indistinguishable until much later.
    """
    if item_id is None:
        return list(rows)
    for index, row in enumerate(rows):
        if row["id"] == item_id:
            return list(rows[: index + 1])
    raise absent()


async def fork_session(
    store: SessionStore, account: str, session: str, item_id: str | None = None
) -> dict[str, Any]:
    """Create a new session holding a copy of ``session`` up to ``item_id``.

    The fork starts idle, unarchived and with its spend at zero: it has said nothing and cost
    nothing yet, whatever the session it came from was doing at the time. It keeps the
    original items' timestamps, because the conversation happened when it happened.

    ``forked_from_item`` records the cut point in the *source* session's own ids, since that
    is what it is a reference into. A fork taken from the end still records the item it ended
    at, which is the only way to say later where the two conversations diverged -- the parent
    keeps growing after the fork is taken.
    """

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        source = session_row(db, account, session)
        rows = db.execute(
            "SELECT * FROM items WHERE session_id=? ORDER BY seq", (session,)
        ).fetchall()
        copied = _prefix(rows, item_id)
        child = identifier("ses")
        now = time.time()
        cut = item_id if item_id is not None else (copied[-1]["id"] if copied else None)
        db.execute(
            _NEW_SESSION,
            (
                child,
                account,
                source["profile"],
                source["title"],
                IDLE,
                source["model"],
                source["thinking_config"],
                source["persona"],
                session,
                cut,
                None,
                None,
                __version__,
                source["input_policy"],
                DURABLE,
                source["permission_mode"],
                source["incognito"],
                now,
                now,
            ),
        )
        _copy_items(db, child, copied)
        value = row_value(session_row(db, account, child))
        detail = {
            "parent_session_id": session,
            "forked_from_item": cut,
            "items": len(copied),
            "workspace_shared": False,
        }
        event_row(db, child, "lucy.session.created", value)
        event_row(db, child, "lucy.session.forked", detail)
        # The source hears about it too: a client watching the conversation somebody just
        # branched can show the new conversation without polling the session list.
        event_row(db, session, "lucy.session.forked", {"session_id": child, **detail})
        return value

    return await store.transaction(apply)


def _copy_items(db: sqlite3.Connection, child: str, rows: Sequence[sqlite3.Row]) -> None:
    """Write the copies, rewriting each parent to the new id of the item it pointed at.

    Written straight into the table rather than through ``item_row`` because a fork is one
    event, not one per item: replaying ``item.added`` for a thousand copied items would tell
    every listener the conversation just happened again.
    """
    remapped: dict[str, str] = {}
    for row in rows:
        fresh = identifier("itm")
        db.execute(
            _COPY_ITEM,
            (
                fresh,
                child,
                row["seq"],
                remapped.get(row["parent_id"]),
                None,
                None,
                row["type"],
                row["role"],
                row["content_json"],
                row["tokens"],
                row["created_at"],
            ),
        )
        remapped[row["id"]] = fresh
