"""What the person has been asked and has not answered reaches the prompt.

`PendingSnapshot`, `_pending_group`, the `pending` quota and the three "waiting for ..."
lines existed and `Sources.pending` was never given anything, so the band was written,
ranked, budgeted, rendered and never once shown.

The shape that costs something, held here as the first test: two facts parked for approval,
a person who types instead of answering, and a later turn that reports what it knows. Before
this source, that turn ended "That is everything."
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from lucy_api.connections.tickets import ConnectionTickets
from lucy_api.context.sources import Sources, StateRequest, gather_live_state
from lucy_api.context.types import BudgetSnapshot, SessionSnapshot
from lucy_api.permissions.approvals import PENDING
from lucy_api.permissions.live import PendingLive
from lucy_api.sessions.schema import SCHEMA

if TYPE_CHECKING:
    from collections.abc import Callable


class _Worker:
    """The store's worker seam, narrow enough to hold one connection."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

    async def call[T](self, work: Callable[[sqlite3.Connection], T]) -> T:
        return work(self._db)


class _Store:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.worker = _Worker(db)


@pytest.fixture
def db() -> sqlite3.Connection:
    """The hub's own schema, not a table written for the test.

    A hand-written table agrees with the query that reads it, whoever is wrong; the real one
    is what the query has to agree with in production.
    """
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def _ask(
    db: sqlite3.Connection,
    identifier: str,
    operation: str,
    description: str,
    *,
    session: str = "ses_a",
    status: str = PENDING,
    at: float = 1.0,
    arguments: object = None,
    input_json: str | None = None,
) -> None:
    """One approval row, written the way `open_approval` writes it."""
    stored = (
        input_json
        if input_json is not None
        else json.dumps({"permission": "notes.write", "arguments": arguments or {}})
    )
    db.execute(
        "INSERT INTO approvals (id, session_id, operation, description, input_json, status,"
        " policy, requested_at) VALUES (?,?,?,?,?,?,?,?)",
        (identifier, session, operation, description, stored, status, "ask", at),
    )


async def test_an_approval_the_person_never_answered_is_still_reported(
    db: sqlite3.Connection,
) -> None:
    """The bug, named. Two facts asked about, neither answered, and a later turn that says
    "that is everything" because nothing in front of it said otherwise."""
    _ask(db, "apr_1", "notes.setFact", "Remember: backend engineer, mostly Python", at=1.0)
    _ask(db, "apr_2", "notes.setFact", "Remember: weekly review on Friday", at=2.0)
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.any
    assert len(snapshot.approvals) == 2
    assert "notes.setFact" in snapshot.approvals[0]
    assert "backend engineer" in snapshot.approvals[0]


async def test_the_line_says_what_the_ask_would_do_not_which_permission_it_needs(
    db: sqlite3.Connection,
) -> None:
    """The second half of the bug. Given only the permission's sentence, Lucy said "I can't
    see what it would record" about an ask whose arguments said exactly that."""
    _ask(
        db,
        "apr_1",
        "notes.setFact",
        "Remember and change notes about you needs approval before it can run.",
        arguments={"title": "Occupation", "body": "Backend engineer, mostly Python."},
    )
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == (
        "notes.setFact -- title: Occupation; body: Backend engineer, mostly Python.",
    )


async def test_only_plain_values_are_carried_from_the_arguments(db: sqlite3.Connection) -> None:
    """A nested object would reach the model as a repr to parse, and a blank says nothing."""
    _ask(
        db,
        "apr_1",
        "workspace.run",
        "Run commands needs approval.",
        arguments={
            "command": "pytest -q",
            "timeout_ms": 60000,
            "env": {"CI": "1"},
            "cwd": "  ",
            "tags": ["a"],
        },
    )
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == ("workspace.run -- command: pytest -q; timeout_ms: 60000",)


@pytest.mark.parametrize(
    "stored",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"permission": "notes.write"}),
        json.dumps({"arguments": "not an object"}),
        json.dumps({"arguments": {"nested": {"only": True}}}),
    ],
)
async def test_an_ask_with_nothing_readable_in_its_arguments_falls_back_to_its_description(
    db: sqlite3.Connection, stored: str
) -> None:
    _ask(db, "apr_1", "notes.setFact", "Remember this", input_json=stored)
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == ("notes.setFact -- Remember this",)


async def test_an_answered_approval_is_not_still_waiting(db: sqlite3.Connection) -> None:
    _ask(db, "apr_1", "notes.setFact", "Remember this", status="granted")
    _ask(db, "apr_2", "notes.forget", "Forget that", status="denied")
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == ()
    assert not snapshot.any


async def test_another_conversations_approval_is_not_this_ones(db: sqlite3.Connection) -> None:
    _ask(db, "apr_1", "notes.setFact", "Somewhere else", session="ses_b")
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == ()


async def test_the_oldest_ask_is_reported_first(db: sqlite3.Connection) -> None:
    """Order is what the person would answer in, so it is the order they were asked."""
    _ask(db, "apr_2", "notes.forget", "second", at=20.0)
    _ask(db, "apr_1", "notes.setFact", "first", at=10.0)
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert [line.split(" -- ")[1] for line in snapshot.approvals] == ["first", "second"]


async def test_a_pathological_session_does_not_load_its_whole_table(
    db: sqlite3.Connection,
) -> None:
    for index in range(40):
        _ask(db, f"apr_{index}", "notes.setFact", f"ask {index}", at=float(index))
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert len(snapshot.approvals) == 8


async def test_an_ask_with_no_description_still_names_the_operation(
    db: sqlite3.Connection,
) -> None:
    _ask(db, "apr_1", "notes.setFact", "")
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == ("notes.setFact",)


async def test_an_ask_with_no_operation_still_says_what_was_asked(
    db: sqlite3.Connection,
) -> None:
    _ask(db, "apr_1", "", "Something the person was asked")
    snapshot = await PendingLive(store=_Store(db)).fetch("ses_a")
    assert snapshot.approvals == ("Something the person was asked",)


# --- connections -------------------------------------------------------------------------


class _Authorization:
    url = "https://provider.invalid/consent"
    expires_at = None


async def test_a_connection_link_the_person_has_not_opened_is_waiting(
    db: sqlite3.Connection,
) -> None:
    tickets = ConnectionTickets()
    tickets.create("acct_a", "personal", "spotify", _Authorization())
    live = PendingLive(store=_Store(db), tickets=tickets, account_id="acct_a", profile="personal")
    snapshot = await live.fetch("ses_a")
    assert snapshot.connections == ("spotify",)


async def test_a_link_the_person_has_already_opened_is_not_waiting_on_them(
    db: sqlite3.Connection,
) -> None:
    """Opened means they are away at the provider. The answer comes back through keyring,
    not through this."""
    tickets = ConnectionTickets()
    ticket = tickets.create("acct_a", "personal", "spotify", _Authorization())
    tickets.open(ticket.id, "acct_a")
    live = PendingLive(store=_Store(db), tickets=tickets, account_id="acct_a", profile="personal")
    assert (await live.fetch("ses_a")).connections == ()


async def test_another_persons_connection_is_not_reported(db: sqlite3.Connection) -> None:
    tickets = ConnectionTickets()
    tickets.create("acct_b", "personal", "spotify", _Authorization())
    live = PendingLive(store=_Store(db), tickets=tickets, account_id="acct_a", profile="personal")
    assert (await live.fetch("ses_a")).connections == ()


async def test_another_profiles_connection_is_not_reported(db: sqlite3.Connection) -> None:
    tickets = ConnectionTickets()
    tickets.create("acct_a", "work", "spotify", _Authorization())
    live = PendingLive(store=_Store(db), tickets=tickets, account_id="acct_a", profile="personal")
    assert (await live.fetch("ses_a")).connections == ()


async def test_no_ticket_registry_means_no_connections_rather_than_a_crash(
    db: sqlite3.Connection,
) -> None:
    assert (await PendingLive(store=_Store(db)).fetch("ses_a")).connections == ()


async def test_an_anonymous_caller_is_told_about_nobodys_connections(
    db: sqlite3.Connection,
) -> None:
    tickets = ConnectionTickets()
    tickets.create("", "personal", "spotify", _Authorization())
    live = PendingLive(store=_Store(db), tickets=tickets, account_id="", profile="personal")
    assert (await live.fetch("ses_a")).connections == ()


async def test_elicitations_are_reported_as_none_rather_than_guessed_at(
    db: sqlite3.Connection,
) -> None:
    """Nothing in the hub writes an elicitation request, so there is no pending one to find.
    Saying none is true; inventing a source for it would not be."""
    _ask(db, "apr_1", "notes.setFact", "Remember this")
    assert (await PendingLive(store=_Store(db)).fetch("ses_a")).elicitations == ()


# --- and it reaches the block ---------------------------------------------------------------


def _request() -> StateRequest:
    return StateRequest(
        now=datetime(2026, 9, 24, tzinfo=UTC),
        session=SessionSnapshot(
            id="ses_a",
            profile="personal",
            title="",
            turn_number=4,
            permission_mode="ask",
        ),
        budget=BudgetSnapshot(used=0, window=200_000),
    )


async def test_the_band_reaches_the_live_state_the_model_reads(db: sqlite3.Connection) -> None:
    """The half that was missing: a source wired to `Sources.pending`, gathered like the
    rest, arriving in the state the assembler renders."""
    _ask(db, "apr_1", "notes.setFact", "Remember: backend engineer")
    state = await gather_live_state(_request(), Sources(pending=PendingLive(store=_Store(db))))
    assert state.pending.any
    assert "backend engineer" in state.pending.approvals[0]


async def test_a_pending_source_that_fails_costs_only_its_own_band(
    db: sqlite3.Connection,
) -> None:
    """The rule the rest of the sources follow: a turn that cannot read its approvals queue
    still answers, and says what it could not see."""

    class Broken:
        async def fetch(self, session_id: str) -> object:
            raise RuntimeError(session_id)

    state = await gather_live_state(_request(), Sources(pending=Broken()))
    assert not state.pending.any
    assert any("pending" in failure.operation for failure in state.failures)
