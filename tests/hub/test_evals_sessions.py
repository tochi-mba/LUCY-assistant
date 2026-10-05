"""A scenario can come back another day: a later turn in a fresh session on the same profile.

The gap, named: every scenario held one session, so "remember that" followed by "what do you
know about me" passed when the model read the answer off its own transcript -- and a fact kept
only for that one conversation, which the next one never sees, passed with it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from eval_fakes import FakeLucy, Play
from test_evals_report import record, report, turn
from test_evals_runner import Held, scenario

from lucy_api.evals.loader import ScenarioError
from lucy_api.evals.markdown import render
from lucy_api.evals.results import PASSED
from lucy_api.evals.runner import _Sessions

COMES_BACK = """summary = "Kept, then recalled another day."

[[turns]]
say = "Remember tea."

[[turns]]
say = "What do I drink?"
new_session = true
"""


def test_a_later_turn_can_start_a_fresh_session_on_the_same_profile() -> None:
    fake = FakeLucy()
    fake.say("Remember tea.", Play(reply="Kept."))
    fake.say("What do I drink?", Play(reply="Tea."))
    held = Held(fake, scenario(COMES_BACK))

    record_ = held.record
    assert record_.outcome == PASSED
    assert record_.session_id == "ses_1", "the scenario is filed under where it started"
    assert [item.session_id for item in record_.turns] == ["ses_1", "ses_2"]
    assert fake.archived == ["ses_1", "ses_2"]
    created = [
        index
        for index, request in enumerate(fake.requests)
        if request.method == "POST" and request.url.path == "/v1/sessions"
    ]
    archived = next(
        index
        for index, request in enumerate(fake.requests)
        if request.method == "PATCH" and request.url.path == "/v1/sessions/ses_1"
    )
    assert created[0] < archived < created[1], "the first conversation is over before the next"
    assert ("event", ("sample", 2, "~ new session ses_2")) in held.recorder.events


def test_a_kept_run_keeps_every_session_it_held() -> None:
    fake = FakeLucy()
    held = Held(fake, scenario(COMES_BACK), keep_sessions=True)
    assert [item.session_id for item in held.record.turns] == ["ses_1", "ses_2"]
    assert fake.archived == []


def test_an_earlier_session_that_will_not_archive_is_reported_not_raised() -> None:
    fake = FakeLucy()
    fake.fail[("PATCH", "/v1/sessions/ses_1")] = 500
    held = Held(fake, scenario(COMES_BACK))
    assert held.record.outcome == PASSED
    assert held.record.reason.startswith("could not archive ses_1: PATCH /v1/sessions/ses_1")
    assert fake.archived == ["ses_2"]


def test_usage_is_every_session_s_added_up() -> None:
    def held(usage: dict[str, Any]) -> Any:
        return SimpleNamespace(usage=usage, close=lambda *, keep: ())

    first = held({"input_tokens": 10, "turn_cache_read_tokens": 4, "model": "a", "flag": True})
    later = [held({"input_tokens": 5, "output_tokens": 2, "model": "b", "flag": True})]
    sessions = _Sessions(first, start=later.pop, keep=False)  # type: ignore[arg-type]
    sessions.another()
    assert sessions.usage() == {
        "input_tokens": 15,
        "turn_cache_read_tokens": 4,
        "output_tokens": 2,
        "model": "b",
        "flag": True,
    }


def test_the_first_turn_cannot_ask_for_a_new_session() -> None:
    text = 'summary = "x"\n\n[[turns]]\nsay = "Hi"\nnew_session = true\n'
    with pytest.raises(ScenarioError, match=r"new_session.*a later one"):
        scenario(text)


def test_a_report_names_each_turn_s_session_only_when_there_was_more_than_one() -> None:
    failing = turn(("a", False), session_id="ses_2", index=2)
    several = render(report([record("alpha", turn(("a", True)), failing)]))
    assert "#### Turn 2: completed in 12.0s, 2 round(s), 0 step(s), in session `ses_2`" in several
    alone = render(report([record("alpha", turn(("a", False), session_id="ses_1"))]))
    assert "in session `" not in alone
