"""Writing to and reading from a transcript, which is a tree pretending to be a list.

Every item carries two orderings and they answer different questions. ``seq`` is the order
things happened in and only ever climbs, because a cursor over an append-only log is correct
only if the numbers behind it never move. ``parent_id`` is what each item is an answer *to*,
and it is allowed to fork: editing a message and regenerating writes a second item with the
**same parent** as the first, so a client can show the two attempts side by side and a
context builder can walk one branch without dragging the other into the prompt.

Collapsing the two -- chaining every item to whatever was written last -- would be simpler
and would quietly make an edit look like a reply to the answer it replaced.

Paging happens here rather than in SQL. The store already reads a session's items in one
go, and re-slicing them in Python keeps one implementation of the cursor rules instead of
two that will disagree the first time one of them is optimised. If a session ever grows
large enough for that to hurt, the fix is a windowed query in the store, not a second
pagination dialect up here.
"""

from __future__ import annotations

from types import EllipsisType
from typing import TYPE_CHECKING, Any

from lucy_api.core.errors import LucyError
from lucy_api.sessions.sql_store import item_row, page, session_row

if TYPE_CHECKING:
    import sqlite3

    from lucy_api.sessions.models import Cursor
    from lucy_api.sessions.sql_store import NewItem, SessionStore

ITEM_TABLE = "items"


def _foreign_parent(session: str) -> LucyError:
    """The parent has to be in the same session, and saying so is the fix.

    The message names the session rather than the item because the item id came from the
    caller: echoing it back would copy a value that was just refused into a log line.
    """
    return LucyError(
        "unknown-parent",
        f"The parent item is not part of session {session}; an item can only answer "
        "another item in the same conversation.",
        404,
    )


async def append_item(
    store: SessionStore,
    account: str,
    session: str,
    item: NewItem,
    parent: str | EllipsisType | None = ...,
) -> dict[str, Any]:
    """Append ``item`` to ``session``, chained to ``parent`` or to the tail of the log.

    A parent from another session is refused rather than stored: a chain that leaves the
    session cannot be walked by anything that reads one session at a time, which is
    everything that reads one at all.
    """
    if isinstance(parent, EllipsisType):
        return await store.append(account, session, item)
    chained = parent

    def apply(db: sqlite3.Connection) -> dict[str, Any]:
        session_row(db, account, session)
        if chained is not None:
            owner = db.execute("SELECT session_id FROM items WHERE id=?", (chained,)).fetchone()
            if owner is None or owner["session_id"] != session:
                raise _foreign_parent(session)
        return item_row(db, session, item, chained)

    return await store.transaction(apply)


async def regenerate_item(
    store: SessionStore, account: str, item_id: str, item: NewItem
) -> dict[str, Any]:
    """Write ``item`` as a sibling of ``item_id``: the same parent, a new place in the log.

    This is edit-and-regenerate. The item being replaced is never touched -- a transcript is
    append-only, and an assistant that can rewrite what it said last week is an assistant
    nobody can audit. Regenerating the *first* item produces another item with no parent,
    which is why the parent is passed through rather than defaulted.
    """
    existing = await store.item(account, item_id)
    parent: str | None = existing["parent_id"]
    return await append_item(store, account, str(existing["session_id"]), item, parent)


async def list_items(
    store: SessionStore, account: str, session: str, cursor: Cursor
) -> dict[str, Any]:
    """One cursor page of a session's transcript, oldest first unless asked otherwise."""
    rows = await store.records(account, session, ITEM_TABLE)
    return page(rows, cursor.limit, cursor.after, cursor.before, cursor.order)
