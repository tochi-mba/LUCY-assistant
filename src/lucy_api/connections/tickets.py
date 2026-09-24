"""Short-lived, subject-bound handles for browser consent redirects."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lucy_api.core.errors import LucyError, absent

if TYPE_CHECKING:
    from collections.abc import Callable

    from lucy_api.clients.keyring import Authorization

DEFAULT_SECONDS = 600.0
TICKET_USED = "connection-ticket-used"


@dataclass(slots=True)
class ConnectionTicket:
    id: str
    account_id: str
    profile: str
    service: str
    provider_url: str
    expires_at: float
    opened: bool = False


class ConnectionTickets:
    """Process-local consent redirects; credentials and provider tokens never enter them."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        issue: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock
        self._issue = issue or (lambda: secrets.token_urlsafe(24))
        self._tickets: dict[str, ConnectionTicket] = {}

    @property
    def count(self) -> int:
        self._prune()
        return len(self._tickets)

    def _prune(self) -> None:
        now = self._clock()
        expired = [
            ticket_id for ticket_id, ticket in self._tickets.items() if ticket.expires_at <= now
        ]
        for ticket_id in expired:
            del self._tickets[ticket_id]

    def create(
        self,
        account_id: str,
        profile: str,
        service: str,
        authorization: Authorization,
    ) -> ConnectionTicket:
        self._prune()
        now = self._clock()
        expires = (
            authorization.expires_at.timestamp()
            if authorization.expires_at is not None
            else now + DEFAULT_SECONDS
        )
        ticket = ConnectionTicket(
            id=self._issue(),
            account_id=account_id,
            profile=profile,
            service=service,
            provider_url=authorization.url,
            expires_at=expires,
        )
        self._tickets[ticket.id] = ticket
        return ticket

    def outstanding(self, account_id: str, profile: str) -> tuple[ConnectionTicket, ...]:
        """Live tickets this person has been handed and not opened yet.

        A ticket that has been opened is no longer waiting on the person -- they are away
        at the provider, and the answer comes back through keyring rather than through
        here. An expired one is pruned first, so nothing stale is reported as pending.
        """
        self._prune()
        return tuple(
            ticket
            for ticket in self._tickets.values()
            if ticket.account_id == account_id and ticket.profile == profile and not ticket.opened
        )

    def read(self, ticket_id: str, account_id: str) -> ConnectionTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.account_id != account_id:
            raise absent()
        if ticket.expires_at <= self._clock():
            del self._tickets[ticket_id]
            raise absent()
        return ticket

    def open(self, ticket_id: str, account_id: str) -> str:
        ticket = self.read(ticket_id, account_id)
        if ticket.opened:
            raise LucyError(
                TICKET_USED,
                "This connection link has already been opened; start a new connection.",
                409,
            )
        ticket.opened = True
        return ticket.provider_url


__all__ = ["ConnectionTicket", "ConnectionTickets"]
