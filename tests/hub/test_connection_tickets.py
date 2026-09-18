"""Provider consent tickets bind the browser subject before redirecting it."""

from datetime import UTC, datetime

import pytest

from lucy_api.clients.keyring import Authorization
from lucy_api.connections.tickets import ConnectionTickets
from lucy_api.core.errors import LucyError


def test_a_ticket_is_subject_bound_short_lived_and_opened_once() -> None:
    now = [1_000.0]
    tickets = ConnectionTickets(clock=lambda: now[0], issue=lambda: "ticket-1")
    authorization = Authorization(
        url="https://provider.test/consent",
        expires_at=datetime.fromtimestamp(1_300, UTC),
    )

    ticket = tickets.create("acct_a", "personal", "spotify", authorization)

    assert ticket.id == "ticket-1"
    assert tickets.read("ticket-1", "acct_a").profile == "personal"
    with pytest.raises(LucyError) as foreign:
        tickets.open("ticket-1", "acct_b")
    assert foreign.value.status == 404
    assert tickets.open("ticket-1", "acct_a") == "https://provider.test/consent"
    with pytest.raises(LucyError, match="already been opened"):
        tickets.open("ticket-1", "acct_a")


def test_an_expired_ticket_is_absent_and_removed() -> None:
    now = [2_000.0]
    tickets = ConnectionTickets(clock=lambda: now[0], issue=lambda: "ticket-2")
    tickets.create(
        "acct_a",
        "personal",
        "spotify",
        Authorization(url="https://provider.test", expires_at=datetime.fromtimestamp(2_001, UTC)),
    )
    now[0] = 2_002.0

    with pytest.raises(LucyError) as expired:
        tickets.read("ticket-2", "acct_a")

    assert expired.value.status == 404
    assert tickets.count == 0


def test_expired_tickets_are_pruned_when_a_new_ticket_is_created() -> None:
    now = [3_000.0]
    issued = iter(("old", "new"))
    tickets = ConnectionTickets(clock=lambda: now[0], issue=lambda: next(issued))
    tickets.create(
        "acct_a",
        "personal",
        "spotify",
        Authorization(url="https://provider.test", expires_at=datetime.fromtimestamp(3_001, UTC)),
    )
    now[0] = 3_002.0

    tickets.create(
        "acct_a",
        "personal",
        "spotify",
        Authorization(url="https://provider.test", expires_at=datetime.fromtimestamp(3_100, UTC)),
    )

    assert tickets.count == 1
