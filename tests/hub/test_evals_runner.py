"""Holding a scenario: the session, the turns, the asks, the clock, and how it all ends.

Every test drives the real runner through the real `HttpHub` against `FakeLucy`, an
in-memory hub on `httpx.MockTransport`. No model is started and nothing sleeps: the clock
only moves when the harness waits.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from eval_fakes import Clock, FakeLucy, Play

from lucy_api.evals.conversation import Pace
from lucy_api.evals.hub import HubError, HubUnreachable
from lucy_api.evals.loader import parse_scenario
from lucy_api.evals.results import ERROR, FAILED, PASSED, SKIPPED, ScenarioRecord
from lucy_api.evals.runner import Job, Plan, Quiet, Runner
from lucy_api.evals.scenario import Scenario

MODEL = "clyde:haiku"


def scenario(text: str, *, name: str = "sample") -> Scenario:
    return parse_scenario(text.encode(), name=name, suite="tests", path=f"tests/{name}.toml")


def one_turn(say: str = "Hello?", extra: str = "", *, summary: str = "One turn.") -> str:
    return f'summary = "{summary}"\n\n[[turns]]\nsay = "{say}"\n{extra}'


class Recorder:
    """An observer that writes down what it was told."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def started(self, job: Job, number: int, total: int) -> None:
        self.events.append(("started", (job.scenario.name, job.model, job.repeat, number, total)))

    def turn_finished(self, job: Job, turn: Any) -> None:
        self.events.append(("turn", (job.scenario.name, turn.index, turn.status)))

    def finished(self, record: ScenarioRecord) -> None:
        self.events.append(("finished", (record.name, record.outcome)))


class Held:
    """One run against a fake hub, and everything a test wants to look at afterwards."""

    def __init__(self, fake: FakeLucy, *scenarios: Scenario, **plan: Any) -> None:
        self.clock = Clock()
        self.recorder = Recorder()
        runner = Runner(
            fake.hub(),
            pace=Pace(clock=self.clock, sleep=self.clock.sleep, poll_seconds=1.0),
            observer=self.recorder,
        )
        self.records: list[ScenarioRecord] = []
        plan.setdefault("models", (MODEL,))
        self.stopped = runner.run(Plan(scenarios=scenarios, **plan), self.records.append)

    @property
    def record(self) -> ScenarioRecord:
        return self.records[0]

    def failing(self) -> list[str]:
        return [check.name for check in self.record.checks if not check.passed]


def posted(fake: FakeLucy, path: str) -> list[dict[str, Any]]:
    return [
        json.loads(request.content)
        for request in fake.requests
        if request.method == "POST" and request.url.path == path
    ]


# --------------------------------------------------------------------------------------
# A conversation that goes as expected
# --------------------------------------------------------------------------------------


def test_a_conversation_that_goes_as_expected_passes_and_is_archived() -> None:
    fake = FakeLucy()
    fake.say(
        "Hello?",
        Play(reply="Hello! I can help.", running_polls=2, tokens=(1200, 64, 800), iterations=2),
    )
    held = Held(fake, scenario(one_turn(extra="[turns.expect]\nreply_matches = ['help']\n")))

    record = held.record
    assert held.stopped is None
    assert record.outcome == PASSED
    assert record.reason == ""
    assert record.session_id == "ses_1"
    assert record.scenario == "tests/sample"
    assert record.model == MODEL
    assert record.repeat == 1
    assert posted(fake, "/v1/sessions") == [
        {
            "title": "[eval] sample",
            "model": MODEL,
            "profile": "personal",
            "permission_mode": "ask",
            "incognito": False,
            "input_policy": "enqueue",
        }
    ]
    turn = record.turns[0]
    assert turn.status == "completed"
    assert turn.termination == "success"
    assert turn.reply == "Hello! I can help."
    assert turn.said == "Hello?"
    assert turn.iterations == 2
    assert (turn.input_tokens, turn.output_tokens, turn.cache_read_tokens) == (1200, 64, 800)
    assert turn.seconds == 3.0
    assert held.clock.slept == [1.0, 1.0, 1.0]
    assert record.seconds == 3.0
    assert record.usage == {"input_tokens": 0, "turn_cache_read_tokens": 800}
    assert fake.archived == ["ses_1"]
    assert fake.cancelled == []
    assert held.recorder.events == [
        ("started", ("sample", MODEL, 1, 1, 1)),
        ("turn", ("sample", 1, "completed")),
        ("finished", ("sample", PASSED)),
    ]


def test_the_session_takes_the_scenario_s_mode_and_the_run_s_profile() -> None:
    fake = FakeLucy()
    text = 'permission_mode = "plan"\nincognito = true\n' + one_turn()
    Held(fake, scenario(text), profile="work")
    body = posted(fake, "/v1/sessions")[0]
    assert body["permission_mode"] == "plan"
    assert body["incognito"] is True
    assert body["profile"] == "work"


def test_a_failed_check_fails_the_scenario() -> None:
    fake = FakeLucy()
    fake.say("Hello?", Play(reply="0 of 200,000 tokens"))
    held = Held(fake, scenario(one_turn(extra="[turns.expect]\nreply_avoids = ['\\b0 of']\n")))
    assert held.record.outcome == FAILED
    assert held.failing() == [r"turn 1: reply avoids /\b0 of/"]


def test_a_transcript_longer_than_a_page_is_read_to_the_end() -> None:
    fake = FakeLucy()
    fake.page = 2
    fake.say(
        "Hello?",
        Play(
            reply="All three.",
            ran=(("notes.search", "ok"), ("notes.search", "ok"), ("notes.search", "ok")),
        ),
    )
    held = Held(fake, scenario(one_turn(extra="[turns.expect]\nran = ['notes.search']\n")))
    assert held.record.outcome == PASSED
    assert len(held.record.turns[0].results) == 3
    afters = [
        request.url.params.get("after")
        for request in fake.requests
        if request.url.path.endswith("/items")
    ]
    assert afters[0] is None
    assert "itm_ses_1_2" in afters


def test_a_hub_that_does_not_report_cache_reads_records_none() -> None:
    fake = FakeLucy()
    fake.usage_has_cache = False
    assert Held(fake, scenario(one_turn())).record.turns[0].cache_read_tokens is None


def test_every_model_and_repeat_gets_its_own_session() -> None:
    fake = FakeLucy()
    first = scenario(one_turn(), name="first")
    second = scenario(
        'summary = "Two."\n[[turns]]\nsay = "a"\n[[turns]]\nsay = "b"\n', name="second"
    )
    plan = Plan(scenarios=(first, second), models=("clyde:haiku", "clyde:sonnet"), repeat=2)
    assert plan.prompts == 12
    assert [(job.model, job.scenario.name, job.repeat) for job in plan.jobs()] == [
        ("clyde:haiku", "first", 1),
        ("clyde:haiku", "first", 2),
        ("clyde:haiku", "second", 1),
        ("clyde:haiku", "second", 2),
        ("clyde:sonnet", "first", 1),
        ("clyde:sonnet", "first", 2),
        ("clyde:sonnet", "second", 1),
        ("clyde:sonnet", "second", 2),
    ]
    held = Held(fake, first, second, models=("clyde:haiku", "clyde:sonnet"), repeat=2)
    assert [record.outcome for record in held.records] == [PASSED] * 8
    assert [body["model"] for body in posted(fake, "/v1/sessions")] == [
        "clyde:haiku",
        "clyde:haiku",
        "clyde:haiku",
        "clyde:haiku",
        "clyde:sonnet",
        "clyde:sonnet",
        "clyde:sonnet",
        "clyde:sonnet",
    ]
    assert len(fake.archived) == 8


def test_a_runner_with_no_observer_runs_quietly() -> None:
    fake = FakeLucy()
    runner = Runner(fake.hub(), pace=Pace(clock=Clock(), sleep=lambda _: None))
    records: list[ScenarioRecord] = []
    assert (
        runner.run(Plan(scenarios=(scenario(one_turn()),), models=(MODEL,)), records.append) is None
    )
    assert records[0].outcome == PASSED
    quiet = Quiet()
    quiet.started(Job(scenario(one_turn()), MODEL, 1), 1, 1)
    quiet.turn_finished(Job(scenario(one_turn()), MODEL, 1), records[0].turns[0])
    quiet.finished(records[0])


# --------------------------------------------------------------------------------------
# Answering asks
# --------------------------------------------------------------------------------------


def test_yes_approves_each_ask_once_and_the_work_runs() -> None:
    fake = FakeLucy()
    fake.say(
        "Remember tea.",
        Play(reply="Saved.", asks=("notes.setFact",), ran=(("notes.setFact", "ok"),)),
    )
    text = one_turn(
        "Remember tea.", "[turns.expect]\nran = ['notes.setFact']\napprovals = ['notes.setFact']\n"
    )
    held = Held(fake, scenario(text))
    assert held.record.outcome == PASSED
    assert fake.answered == [
        {
            "type": "input.approval",
            "approval_id": "apr_trn_1_1",
            "approved": True,
            "lifetime": "once",
        }
    ]
    assert held.record.turns[0].asks[0].answer == "approved"
    assert held.record.turns[0].asks[0].arguments == "title=Drink, body=Prefers tea"


def test_yes_session_approves_for_the_rest_of_the_conversation() -> None:
    fake = FakeLucy()
    fake.say("Go.", Play(asks=("workspace.write",), ran=(("workspace.write", "ok"),)))
    Held(fake, scenario(one_turn("Go.", 'approve = "yes-session"\n')))
    assert [answer["lifetime"] for answer in fake.answered] == ["session"]
    assert [answer["approved"] for answer in fake.answered] == [True]


def test_no_refuses_and_the_refused_work_does_not_run() -> None:
    fake = FakeLucy()
    fake.say(
        "Delete it.",
        Play(
            reply="Deleted.",
            denied_reply="I did not delete anything.",
            asks=("workspace.delete",),
            ran=(("workspace.delete", "ok"),),
        ),
    )
    text = one_turn(
        "Delete it.", 'approve = "no"\n[turns.expect]\nnot_ran = ["workspace.delete"]\n'
    )
    held = Held(fake, scenario(text))
    assert held.record.outcome == PASSED
    assert fake.answered[0]["approved"] is False
    assert fake.answered[0]["lifetime"] == "once"
    turn = held.record.turns[0]
    assert turn.reply == "I did not delete anything."
    assert turn.results[0].status == "denied"


def test_every_ask_on_a_turn_is_answered_one_request_at_a_time() -> None:
    fake = FakeLucy()
    fake.say(
        "Build it.",
        Play(
            asks=("workspace.write", "workspace.run"),
            ran=(("workspace.write", "ok"), ("workspace.run", "ok")),
        ),
    )
    held = Held(
        fake,
        scenario(
            one_turn("Build it.", "[turns.expect]\nran = ['workspace.write', 'workspace.run']\n")
        ),
    )
    assert held.record.outcome == PASSED
    assert [answer["approval_id"] for answer in fake.answered] == ["apr_trn_1_1", "apr_trn_1_2"]
    assert all(
        len(json.loads(request.content)["events"]) == 1
        for request in fake.requests
        if request.url.path.endswith("/inputs")
    )


def test_ignore_leaves_the_ask_parked_and_the_harness_cancels_it_afterwards() -> None:
    fake = FakeLucy()
    fake.say("Remember tea.", Play(asks=("notes.setFact",), ran=(("notes.setFact", "ok"),)))
    fake.say("Anything waiting?", Play(reply="Yes: saving that you prefer tea."))
    text = (
        'summary = "Ignored."\n'
        '[[turns]]\nsay = "Remember tea."\napprove = "ignore"\n'
        "[turns.expect]\napprovals = ['notes.setFact']\n"
        '[[turns]]\nsay = "Anything waiting?"\n'
        "[turns.expect]\nreply_matches = ['tea']\n"
    )
    held = Held(fake, scenario(text))
    assert held.record.outcome == PASSED
    assert fake.answered == []
    first, second = held.record.turns
    assert first.status == "input_required"
    assert first.reply == ""
    assert first.asks[0].answer == ""
    assert second.status == "completed"
    assert fake.cancelled == ["trn_1"]
    assert fake.archived == ["ses_1"]


def test_a_park_with_nothing_to_answer_ends_the_wait() -> None:
    fake = FakeLucy()
    fake.say("Hello?", Play(park_without_asks=True))
    held = Held(fake, scenario(one_turn()))
    assert held.record.outcome == FAILED
    assert held.record.turns[0].status == "input_required"
    assert fake.answered == []


def test_a_turn_waiting_on_a_connection_rests_and_is_cancelled_afterwards() -> None:
    fake = FakeLucy()
    fake.say("Play jazz.", Play(reply="Connect music first.", rest_as="auth_required"))
    text = one_turn("Play jazz.", '[turns.expect]\nstatus = "auth_required"\n')
    held = Held(fake, scenario(text))
    assert held.record.outcome == PASSED
    assert fake.cancelled == ["trn_1"]


def test_keep_sessions_leaves_the_session_as_it_was() -> None:
    fake = FakeLucy()
    fake.say("Remember tea.", Play(asks=("notes.setFact",)))
    text = one_turn("Remember tea.", 'approve = "ignore"\n')
    held = Held(fake, scenario(text), keep_sessions=True)
    assert held.record.outcome == PASSED
    assert fake.cancelled == []
    assert fake.archived == []


# --------------------------------------------------------------------------------------
# Requirements
# --------------------------------------------------------------------------------------


def test_a_scenario_whose_capability_is_not_installed_is_skipped_before_anything_exists() -> None:
    fake = FakeLucy()
    held = Held(fake, scenario('requires = ["music"]\n' + one_turn()))
    assert held.record.outcome == SKIPPED
    assert held.record.reason == "music is not installed on this hub"
    assert fake.sessions == {}


def test_a_capability_that_is_not_ready_says_why_it_skipped() -> None:
    fake = FakeLucy()
    fake.capabilities = [
        {"id": "research", "usable": False, "state": "not_connected", "detail": "sign in first"},
        {"id": "notes", "usable": False},
    ]
    held = Held(fake, scenario('requires = ["research", "notes"]\n' + one_turn()))
    assert held.record.outcome == SKIPPED
    assert held.record.reason == "research is not_connected: sign in first; notes is not ready"


def test_a_ready_capability_lets_the_scenario_run() -> None:
    fake = FakeLucy()
    held = Held(fake, scenario('requires = ["research"]\n' + one_turn()))
    assert held.record.outcome == PASSED


# --------------------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------------------


def test_a_turn_that_never_rests_is_cancelled_and_the_rest_are_not_sent() -> None:
    fake = FakeLucy()
    fake.say("Build it.", Play(forever=True))
    text = 'summary = "Stuck."\n[[turns]]\nsay = "Build it."\n[[turns]]\nsay = "And now?"\n'
    held = Held(fake, scenario(text), timeout=3.0)
    record = held.record
    assert record.outcome == FAILED
    assert record.reason == "turn 1 did not come to rest in 3s, so 1 later turn(s) were not sent"
    assert len(record.turns) == 1
    assert record.turns[0].timed_out is True
    assert record.turns[0].status == "running"
    assert "turn 1: came to rest before the timeout" in held.failing()
    assert held.clock.slept == [1.0, 1.0, 1.0]
    assert len(posted(fake, "/v1/sessions/ses_1/inputs")) == 1
    assert fake.cancelled == ["trn_1", "trn_1"]


def test_a_turn_s_own_timeout_wins_and_a_last_turn_timing_out_leaves_no_reason() -> None:
    fake = FakeLucy()
    fake.say("Build it.", Play(forever=True))
    held = Held(fake, scenario(one_turn("Build it.", "timeout_seconds = 2\n")), timeout=300.0)
    assert held.clock.slept == [1.0, 1.0]
    assert held.record.reason == ""
    assert held.record.outcome == FAILED


# --------------------------------------------------------------------------------------
# When the hub says no
# --------------------------------------------------------------------------------------


def test_a_session_the_hub_will_not_create_is_an_error_and_the_run_goes_on() -> None:
    fake = FakeLucy()
    fake.fail[("POST", "/v1/sessions")] = 503
    held = Held(fake, scenario(one_turn(), name="first"), scenario(one_turn(), name="second"))
    assert [record.outcome for record in held.records] == [ERROR, ERROR]
    assert held.stopped is None
    assert held.record.reason == (
        "stopped before a session existed: POST /v1/sessions answered 503: "
        "/v1/sessions is failing on purpose"
    )


def test_readiness_the_hub_cannot_report_is_an_error() -> None:
    fake = FakeLucy()
    fake.fail[("GET", "/v1/capabilities")] = 500
    held = Held(fake, scenario('requires = ["research"]\n' + one_turn()))
    assert held.record.outcome == ERROR
    assert held.record.reason.startswith("stopped before a session existed: GET /v1/capabilities")


def test_a_refused_token_stops_the_run_and_every_later_job_is_recorded() -> None:
    fake = FakeLucy()
    fake.fail[("POST", "/v1/sessions")] = 401
    held = Held(fake, scenario(one_turn(), name="first"), scenario(one_turn(), name="second"))
    assert isinstance(held.stopped, HubError)
    assert held.stopped.status == 401
    assert held.stopped.fatal is True
    first, second = held.records
    assert first.outcome == ERROR
    assert second.outcome == ERROR
    assert second.reason.startswith(
        "not run: the run stopped early: POST /v1/sessions answered 401"
    )
    assert [event for event, _ in held.recorder.events] == ["started", "finished"]


def test_a_hub_that_vanishes_mid_conversation_keeps_what_was_said_so_far() -> None:
    fake = FakeLucy()
    fake.fail[("GET", "/v1/turns/trn_2")] = httpx.ConnectError("gone")
    fake.fail[("PATCH", "/v1/sessions/ses_1")] = httpx.ConnectError("gone")
    text = 'summary = "Two."\n[[turns]]\nsay = "a"\n[[turns]]\nsay = "b"\n'
    held = Held(fake, scenario(text, name="first"), scenario(one_turn(), name="second"))
    assert isinstance(held.stopped, HubUnreachable)
    first, second = held.records
    assert first.outcome == ERROR
    assert len(first.turns) == 1
    assert first.reason == (
        "stopped mid-conversation: cannot reach Lucy at http://127.0.0.1:8000; "
        "could not archive ses_1: cannot reach Lucy at http://127.0.0.1:8000"
    )
    assert second.reason.startswith("not run: the run stopped early: cannot reach Lucy")


def test_tidying_up_that_fails_is_reported_not_raised() -> None:
    fake = FakeLucy()
    fake.say("Remember tea.", Play(asks=("notes.setFact",)))
    fake.fail[("POST", "/v1/turns/trn_1/cancel")] = 500
    held = Held(fake, scenario(one_turn("Remember tea.", 'approve = "ignore"\n')))
    assert held.record.outcome == PASSED
    assert held.record.reason == (
        "could not cancel trn_1: POST /v1/turns/trn_1/cancel answered 500: "
        "/v1/turns/trn_1/cancel is failing on purpose"
    )
    assert fake.archived == ["ses_1"]


def test_a_failed_turn_is_recorded_with_its_errors_and_what_the_model_was_shown() -> None:
    fake = FakeLucy()
    fake.say(
        "Look it up.",
        Play(
            reply="",
            status="failed",
            termination="error_during_execution",
            ran=(("notes.search", "error"),),
            summary="reported by notes: nothing matched",
            error="memory answered 503",
            errors=("empty_reply",),
        ),
    )
    text = one_turn(
        "Look it up.",
        '[turns.expect]\nstatus = "failed"\ntermination = "error_during_execution"\n'
        "[turns.expect.results.'notes.search']\nmatches = ['nothing matched', '503']\n",
    )
    held = Held(fake, scenario(text))
    assert held.record.outcome == PASSED
    turn = held.record.turns[0]
    assert turn.errors == ("empty_reply",)
    assert turn.results[0].error == "memory answered 503"
    assert turn.reply == ""
