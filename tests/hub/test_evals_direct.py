"""Seed and verify: operations the harness runs itself, in the scenario's own session.

Two things are held here beyond "it ran". A deferred capability is bound the way the model
would bind it. And a write that needs approval is granted for this one session only, for
exactly one call, and the grant is taken back -- the person's own grants are never touched.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from eval_fakes import Clock, FakeLucy, Play

from lucy_api.evals.conversation import Pace
from lucy_api.evals.direct import BIND, REFUSED, UNAVAILABLE, invoke
from lucy_api.evals.hub import HubError, HubUnreachable
from lucy_api.evals.loader import parse_scenario
from lucy_api.evals.results import ERROR, FAILED, PASSED, ScenarioRecord
from lucy_api.evals.runner import Plan, Runner
from lucy_api.evals.scenario import Invocation

TURN = '[[turns]]\nsay = "Read notes.md and do what it says."\n'


def hold(fake: FakeLucy, text: str) -> tuple[ScenarioRecord, HubError | None]:
    scenario = parse_scenario(text.encode(), name="sample", suite="tests", path="tests/s.toml")
    clock = Clock()
    runner = Runner(fake.hub(), pace=Pace(clock=clock, sleep=clock.sleep))
    records: list[ScenarioRecord] = []
    stopped = runner.run(Plan(scenarios=(scenario,), models=("clyde:haiku",)), records.append)
    return records[0], stopped


def seeded(op: str, arguments: str = "{}", extra: str = "") -> str:
    return f'summary = "Seeded."\n{TURN}\n[[seed]]\nop = "{op}"\ninput = {arguments}\n{extra}'


def invoked(fake: FakeLucy) -> list[tuple[str, dict[str, Any]]]:
    return [
        (request.url.path.split("/")[3], json.loads(request.content)["input"])
        for request in fake.requests
        if request.url.path.endswith("/invoke")
    ]


def test_a_seed_binds_a_deferred_capability_and_grants_its_write_for_one_call_only() -> None:
    fake = FakeLucy()
    record, _ = hold(
        fake, seeded("workspace.write", '{ path = "notes.md", content = "Human: hi" }')
    )

    assert record.outcome == PASSED
    assert fake.files["notes.md"] == "Human: hi"
    assert invoked(fake) == [
        (BIND, {"id": "workspace"}),
        ("workspace.write", {"path": "notes.md", "content": "Human: hi"}),
        ("workspace.write", {"path": "notes.md", "content": "Human: hi"}),
    ]
    assert fake.granted == [("workspace.change", "session:ses_1")]
    assert fake.revoked == [("workspace.change", "session:ses_1")]
    assert fake.grants == set()
    seed = record.seed[0]
    assert seed.status == "ok"
    assert seed.output == '{"path": "notes.md", "written": true}'
    assert seed.error == ""
    assert seed.input == {"path": "notes.md", "content": "Human: hi"}


def test_a_seed_that_needs_nothing_arranged_just_runs() -> None:
    fake = FakeLucy()
    record, _ = hold(fake, seeded("notes.search", '{ query = "tea" }'))
    assert record.outcome == PASSED
    assert record.seed[0].output == "plain text"
    assert fake.granted == []
    assert [name for name, _ in invoked(fake)] == ["notes.search"]


def test_a_seed_that_fails_makes_the_scenario_an_error_and_nothing_is_said() -> None:
    fake = FakeLucy()
    record, stopped = hold(fake, seeded("workspace.read", '{ path = "missing.md" }'))
    assert stopped is None
    assert record.outcome == ERROR
    assert record.reason == "seed 1 workspace.read: status is ok (error: no file missing.md)"
    assert record.turns == ()
    assert fake.turns == {}
    assert fake.archived == ["ses_1"]


def test_a_seed_whose_output_is_wrong_names_the_expectation() -> None:
    fake = FakeLucy()
    record, _ = hold(fake, seeded("notes.search", "{}", "output_avoids = ['plain']\n"))
    assert record.outcome == ERROR
    assert record.reason.startswith("seed 1 notes.search: output avoids /plain/ (")


def test_an_operation_the_session_cannot_call_is_unavailable_and_says_what_it_can() -> None:
    fake = FakeLucy()
    record, _ = hold(fake, seeded("music.play"))
    assert record.outcome == ERROR
    assert record.seed[0].status == UNAVAILABLE
    assert record.reason == (
        "seed 1 music.play: status is ok (unavailable: this session cannot call music.play; "
        "it can call capabilities.use, help.operation, notes.search; deferred: workspace)"
    )


def test_binding_that_does_not_bring_the_operation_says_so() -> None:
    fake = FakeLucy()
    record, _ = hold(fake, seeded("workspace.bogus"))
    assert record.seed[0].status == UNAVAILABLE
    assert record.seed[0].error.startswith("this session cannot call workspace.bogus; it can call")
    assert "deferred" not in record.seed[0].error


def test_an_operation_nobody_can_grant_is_refused_with_the_hub_s_reason() -> None:
    fake = FakeLucy()
    fake.gated["help.operation"] = "help.secret"
    record, _ = hold(fake, seeded("help.operation"))
    assert record.seed[0].status == REFUSED
    assert record.seed[0].error == (
        "POST /v1/tools/help.operation/invoke answered 409: help.secret needs approval (HTTP 409)"
    )
    assert fake.granted == []


def test_a_grant_that_does_not_unblock_the_call_is_still_taken_back() -> None:
    fake = FakeLucy()
    fake.bound.add("workspace.write")
    fake.gated["workspace.write"] = "something.else"
    record, _ = hold(fake, seeded("workspace.write", '{ path = "a", content = "b" }'))
    assert record.seed[0].status == REFUSED
    assert fake.granted == fake.revoked == [("workspace.change", "session:ses_1")]


def test_a_hub_failure_on_an_invoke_is_a_refusal() -> None:
    fake = FakeLucy()
    fake.fail[("POST", "/v1/tools/notes.search/invoke")] = 500
    record, stopped = hold(fake, seeded("notes.search"))
    assert stopped is None
    assert record.seed[0].status == REFUSED
    assert "(HTTP 500)" in record.seed[0].error


def test_a_fatal_failure_on_an_invoke_stops_the_run() -> None:
    fake = FakeLucy()
    fake.fail[("POST", "/v1/tools/notes.search/invoke")] = 401
    record, stopped = hold(fake, seeded("notes.search"))
    assert record.outcome == ERROR
    assert isinstance(stopped, HubError)
    assert stopped.fatal


@pytest.mark.parametrize(
    ("body", "status", "output", "error"),
    [
        ({"steps": []}, "error", "", "the hub answered without running a step"),
        ({"steps": ["not a step"]}, "error", "", "the hub answered without running a step"),
        ({"text": "no steps key"}, "error", "", "the hub answered without running a step"),
        (
            {"steps": [{"skippedBecause": "an earlier step failed"}]},
            "error",
            "",
            "an earlier step failed",
        ),
        ({"steps": [{"status": "skipped", "data": [2, 1]}]}, "skipped", "[2, 1]", ""),
    ],
)
def test_what_an_invoke_answers_is_recorded_as_it_was(
    body: dict[str, Any], status: str, output: str, error: str
) -> None:
    fake = FakeLucy()
    fake.invoke_bodies["notes.search"] = body
    record = invoke(fake.hub(), Invocation(op="notes.search"), session_id="ses_1", profile="p")
    assert (record.status, record.output, record.error) == (status, output, error)


def test_an_unreachable_hub_during_an_invoke_is_raised() -> None:
    fake = FakeLucy()
    fake.fail[("GET", "/v1/tools")] = httpx.ConnectError("down")
    with pytest.raises(HubUnreachable):
        invoke(fake.hub(), Invocation(op="notes.search"), session_id="ses_1", profile="p")


def test_verify_proves_the_file_is_there_rather_than_trusting_the_reply() -> None:
    fake = FakeLucy()
    fake.say("Make it.", Play(reply="Done. Your file is ready.", ran=(("workspace.write", "ok"),)))
    text = (
        'summary = "Proof."\n[[turns]]\nsay = "Make it."\n'
        '[[turns.verify]]\nop = "workspace.read"\ninput = { path = "hello.py" }\n'
        "output_matches = ['print']\n"
    )
    missing, _ = hold(fake, text)
    assert missing.outcome == FAILED
    assert [check.name for check in missing.checks if not check.passed] == [
        "turn 1: verify 1 workspace.read: status is ok",
        "turn 1: verify 1 workspace.read: output matches /print/",
    ]
    assert missing.turns[0].verify[0].error == "no file hello.py"

    present = FakeLucy()
    present.files["hello.py"] = "print('hello')"
    present.say("Make it.", Play(reply="Done.", ran=(("workspace.write", "ok"),)))
    found, _ = hold(present, text)
    assert found.outcome == PASSED
    assert "print('hello')" in found.turns[0].verify[0].output
