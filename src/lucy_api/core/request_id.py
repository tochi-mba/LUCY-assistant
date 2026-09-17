"""The request id, bound once at the edge and readable from anywhere.

Every failure the hub returns carries an id the caller is told to quote, and the response
that carries it is built inside an exception handler that never sees the request. Threading
the id through every signature to reach that one place would put an HTTP parameter on
functions that have nothing else to do with HTTP.

A context variable is task-local, so two concurrent requests can never read each other's
id. That matters more than it sounds here: the id is the only thing tying a 500 to the log
line that says what actually went wrong, and one crossed wire sends whoever is debugging to
the wrong request -- in a service where the wrong request is somebody else's conversation.

The module is called ``request_id`` rather than ``context``, which is what the siblings call
it, because in this repository ``lucy_api.context`` is the model's context window. Two
things named "context" one import apart would cost a reader more than the departure does.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

_request_id: ContextVar[str | None] = ContextVar("lucy_request_id", default=None)


def new_request_id() -> str:
    """Return a fresh request id."""
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    """Return the current request id, or ``None`` outside a request."""
    return _request_id.get()


@contextmanager
def bind_request_id(request_id: str) -> Iterator[str]:
    """Bind ``request_id`` for the block, restoring the previous value afterwards."""
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _request_id.reset(token)
