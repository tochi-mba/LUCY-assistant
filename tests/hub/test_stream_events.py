"""The taxonomy's tests are about the taxonomy as a whole, not about any name in it.

Asserting that `SESSION_CREATED == "lucy.session.created"` would test nothing: it is the
same fact written twice, and it would go on passing on the day somebody added
`lucy.session.was_created` beside it. What is worth defending is the set -- that every name
in it obeys one grammar, that no two names are the same string, and that the index and the
constants cannot drift apart. Those are the properties a client depends on and the ones a
hundred and sixty-nine hand-written lines will eventually break.

The catalogue is open, so the tests are careful about which half they pin. A name outside
the grammar is a bug; a name outside the catalogue is Tuesday.
"""

from __future__ import annotations

from collections import Counter

from lucy_api.sessions.models import TERMINAL
from lucy_api.stream import events

PLAN_GROUPS = (
    "Session",
    "Turn",
    "Model",
    "Content",
    "Plan and tools",
    "Capabilities and connections",
    "Approvals",
    "Agents",
    "Work",
    "Journal",
    "Memory",
    "Context",
    "Workspace",
    "Files",
    "MCP",
    "Usage",
    "Security",
    "Stream",
)

DECLARED = {
    name: value
    for name, value in vars(events).items()
    if name.isupper() and isinstance(value, str) and value.startswith("lucy.")
}
"""Every event constant, read off the module rather than listed again here."""


def test_every_declared_event_name_obeys_the_grammar() -> None:
    wrong = {name: value for name, value in DECLARED.items() if not events.is_well_formed(value)}

    assert wrong == {}
    assert len(DECLARED) > 150


def test_no_two_constants_name_the_same_event() -> None:
    """A duplicate is invisible in review and permanent once a client has seen both."""
    repeated = [value for value, count in Counter(DECLARED.values()).items() if count > 1]

    assert repeated == []

    listed = [name for group in events.GROUPS.values() for name in group]
    assert [value for value, count in Counter(listed).items() if count > 1] == []


def test_the_index_lists_every_constant_and_invents_none() -> None:
    """`GROUPS` is what documentation and tooling read, so it has to be the whole truth."""
    assert set(DECLARED.values()) == events.EVENT_TYPES
    assert len(events.EVENT_TYPES) == len(DECLARED)


def test_the_catalogue_is_grouped_exactly_as_the_plan_groups_it() -> None:
    assert tuple(events.GROUPS) == PLAN_GROUPS
    assert set(events.GROUPS["Stream"]) == events.TRANSPORT_TYPES
    assert events.STREAM_HEARTBEAT in events.TRANSPORT_TYPES


def test_a_malformed_name_is_a_bug_while_an_unknown_one_is_only_unknown() -> None:
    """The grammar is closed and the catalogue is open, which is the whole contract."""
    assert events.is_well_formed(events.SESSION_CREATED)
    assert events.is_well_formed("lucy.model.request.started")
    assert events.is_well_formed("lucy.acme.widget.polished")
    assert not events.is_declared("lucy.acme.widget.polished")

    assert not events.is_well_formed("lucy.session")
    assert not events.is_well_formed("lucy.a.b.c.d")
    assert not events.is_well_formed("session.created")
    assert not events.is_well_formed("lucy.Session.created")
    assert not events.is_well_formed("lucy.session.created ")
    assert not events.is_well_formed("lucy.session.9created")


def test_the_names_the_session_store_already_writes_are_in_the_catalogue() -> None:
    """Two modules agree through a string, so something has to check that they still do.

    `finish_turn` composes `lucy.turn.<status>`, and the statuses that end a turn are the
    three below. The non-terminal ones it can also be handed are deliberately not asserted:
    the plan puts `requires_action` and `auth_required` on the session rather than the turn,
    so `lucy.turn.input_required` is well formed, undeclared, and exactly the case clients
    are told to ignore.
    """
    for name in (
        events.SESSION_CREATED,
        events.SESSION_UPDATED,
        events.SESSION_HARNESS_VERSION_CHANGED,
        events.CONTENT_ITEM_ADDED,
    ):
        assert events.is_declared(name)

    for status in sorted(TERMINAL):
        assert events.is_declared("lucy.turn." + status)


def test_an_envelope_carries_the_ids_that_apply_and_omits_the_ones_that_do_not() -> None:
    """A null would teach a client that absence is a value it has to handle."""
    bare = events.Event(
        type=events.SESSION_CREATED,
        sequence_number=1,
        event_id="evt_1",
        session_id="ses_1",
        created_at=1.5,
    )

    body = bare.envelope()

    assert body == {
        "type": events.SESSION_CREATED,
        "sequence_number": 1,
        "event_id": "evt_1",
        "session_id": "ses_1",
        "created_at": 1.5,
        "data": {},
    }

    full = events.Event(
        type=events.TOOL_STARTED,
        sequence_number=2,
        event_id="evt_2",
        session_id="ses_1",
        created_at=2.5,
        data={"operation": "music.play"},
        turn_id="trn_1",
        agent_id="agt_1",
        trace_id="trace_1",
    )

    assert full.envelope() == {
        "type": events.TOOL_STARTED,
        "sequence_number": 2,
        "event_id": "evt_2",
        "session_id": "ses_1",
        "created_at": 2.5,
        "data": {"operation": "music.play"},
        "turn_id": "trn_1",
        "agent_id": "agt_1",
        "trace_id": "trace_1",
    }


def test_an_envelope_copies_the_payload_rather_than_sharing_it() -> None:
    """The envelope is handed to an encoder; mutating it must not edit the stored event."""
    payload = {"count": 1}
    event = events.Event(
        type=events.USAGE_UPDATED,
        sequence_number=3,
        event_id="evt_3",
        session_id="ses_1",
        created_at=3.5,
        data=payload,
    )

    body = event.envelope()
    body["data"]["count"] = 99

    assert payload == {"count": 1}
