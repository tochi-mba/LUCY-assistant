"""The live block shows a queued helper as queued, and names the group a helper is in.

Kept apart from `test_context_state.py`, which is at the file limit.
"""

from __future__ import annotations

from test_context_state import a_state, body_of, entries_of, headline, running_agent


def test_a_queued_helper_is_in_flight_says_it_is_queued_and_names_its_group() -> None:
    """Waiting behind the cap is not running, and the headline does not say it is."""
    state = a_state(
        in_flight=(
            running_agent(group="reviewers", role="reviewer"),
            running_agent(id="a2", status="queued", elapsed_seconds=40.0, group="skeptics"),
        )
    )

    assert entries_of(body_of(state), "in_flight") == [
        "researcher in skeptics - find every caller of the old ingest API - queued 40s",
        "reviewer in reviewers - find every caller of the old ingest API - 2m14s",
    ]
    assert headline(body_of(state), "in_flight") == "1 thing running, 1 queued"


def test_a_finished_member_of_a_group_is_named_with_its_group() -> None:
    state = a_state(
        in_flight=(
            running_agent(status="succeeded", finished_since_last_turn=True, group="reviewers"),
        )
    )

    assert entries_of(body_of(state), "finished") == [
        "researcher in reviewers - find every caller of the old ingest API - succeeded after 2m14s"
    ]
