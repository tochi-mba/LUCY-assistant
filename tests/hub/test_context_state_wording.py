"""What the live block says, held to being true.

Kept apart from `test_context_state.py`, which is at the file limit.
"""

from __future__ import annotations

from test_context_state import (
    a_crowd,
    a_state,
    a_workspace,
    body_of,
    headline,
    running_agent,
)

from lucy_api.context.state import CONFESS_FULL, _Group, _omitted, _Quota


def test_the_session_line_spends_nothing_on_an_id_no_operation_takes() -> None:
    """The bug, named: every round paid about ten tokens for "ses_4f2a", which no operation
    accepts and the model has no use for."""
    assert "ses_4f2a" not in body_of(a_state())


def test_finished_work_is_not_said_to_have_ended_since_the_last_turn() -> None:
    """The bug, named: what ends while the model's own steps run is shown on the next round of
    the same turn, under a heading that said "since your last turn"; and the workspace said
    "changed since your last turn" of `git status`, which is every uncommitted file."""
    state = a_state(
        in_flight=(running_agent(status="done", finished_since_last_turn=True),),
        workspace=a_workspace(changed_files=("old.py",)),
    )
    rendered = body_of(state)
    assert "since your last turn" not in headline(rendered, "finished")
    assert headline(rendered, "workspace").endswith("1 uncommitted file")


def test_a_dropped_group_names_the_call_that_brings_it_back_not_somebody_to_ask() -> None:
    """The bug, named: "dropped for space, ask if you need them" -- and no operation reads the
    live block, so the only one a model could ask was the person."""
    rendered = body_of(a_crowd(), limit=270)
    omitted = headline(rendered, "omitted")

    assert "ask if" not in omitted
    assert "notes.search" in omitted, "memory went, and notes.search finds what it held"


def test_a_dropped_group_with_no_call_to_fetch_it_is_named_without_one() -> None:
    gone = [
        _Group(name="trouble", quota=_Quota(rank=1, ceiling=1, floor=1), headline="x", entries=())
    ]
    assert _omitted(gone, CONFESS_FULL) == "trouble - dropped for space"
