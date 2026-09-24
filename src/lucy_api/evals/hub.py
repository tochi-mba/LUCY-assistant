"""The seam between the harness and a running hub.

The harness is a *client*. It holds conversations over the same public HTTP routes a
person's client uses and never imports the hub's internals -- an import contract says so
-- because the whole reason it exists is that tests which reach inside the hub were green
while real conversations failed. This Protocol names exactly the routes it needs, and
:class:`lucy_api.cli.evals_hub.HttpHub` is the one implementation, built on the same
client, URL and token every other ``lucy`` command resolves.

Every method answers the hub's JSON or raises :class:`HubError`. A failure the run cannot
survive -- the hub gone, or the token refused -- is :attr:`HubError.fatal`, and the runner
stops rather than failing every remaining scenario for the same reason.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Protocol

FATAL_STATUSES = frozenset({HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN})

SESSION_GRANT_PREFIX = "session:"
"""The profile a grant is stored under to last for one conversation.

The hub spells it ``lucy_api.permissions.store.SESSION_PROFILE_PREFIX``; the harness may not
import that, so it repeats it, and the contract test against the real app is what holds the
two together."""


class HubError(Exception):
    """The hub answered, and not with what the harness needed.

    ``status`` is the HTTP status, or 0 when there was no answer at all.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def fatal(self) -> bool:
        """Whether every later request would fail the same way."""
        return self.status in FATAL_STATUSES


class HubUnreachable(HubError):  # noqa: N818 - a state, named as the CLI names it
    """Nothing answered. Always fatal: there is no scenario left that could pass."""

    @property
    def fatal(self) -> bool:
        return True


class Hub(Protocol):
    """The routes a conversation needs, and nothing else."""

    def health(self) -> dict[str, Any]:
        """``GET /healthy``: the version and environment, for the report."""
        ...

    def models(self) -> dict[str, Any]:
        """``GET /v1/models``: which providers are usable, before anything is created."""
        ...

    def capabilities(self, profile: str) -> list[dict[str, Any]]:
        """``GET /v1/capabilities``: readiness, for a scenario's ``requires``."""
        ...

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/sessions``."""
        ...

    def send_message(self, session_id: str, text: str) -> dict[str, Any]:
        """``POST /v1/sessions/{id}/inputs`` with one ``input.message``; answers the turn."""
        ...

    def answer_approval(
        self, session_id: str, approval_id: str, *, approved: bool, lifetime: str
    ) -> dict[str, Any]:
        """``POST /v1/sessions/{id}/inputs`` with one ``input.approval``."""
        ...

    def turn(self, turn_id: str) -> dict[str, Any]:
        """``GET /v1/turns/{id}``."""
        ...

    def cancel_turn(self, turn_id: str) -> dict[str, Any]:
        """``POST /v1/turns/{id}/cancel``. Idempotent on the hub's side."""
        ...

    def items(self, session_id: str, after: str | None) -> dict[str, Any]:
        """One page of ``GET /v1/sessions/{id}/items``, oldest first, after a cursor."""
        ...

    def usage(self, session_id: str) -> dict[str, Any]:
        """``GET /v1/sessions/{id}/usage``."""
        ...

    def tools(self, session_id: str) -> dict[str, Any]:
        """``GET /v1/tools?session_id=``: what is bound, and what is deferred."""
        ...

    def invoke(self, operation: str, arguments: dict[str, Any], session_id: str) -> dict[str, Any]:
        """``POST /v1/tools/{name}/invoke`` in this session. No model, same gate."""
        ...

    def permissions(self, profile: str) -> list[dict[str, Any]]:
        """``GET /v1/permissions``: which permission covers which operation."""
        ...

    def grant(self, permission: str, profile: str) -> None:
        """``PUT /v1/permissions``: allow one permission under one profile."""
        ...

    def revoke(self, permission: str, profile: str) -> None:
        """``DELETE /v1/permissions/{id}``."""
        ...

    def archive(self, session_id: str) -> None:
        """``PATCH /v1/sessions/{id}`` with ``archived: true``. Reversible."""
        ...


__all__ = ["SESSION_GRANT_PREFIX", "Hub", "HubError", "HubUnreachable"]
