"""Steps between turns: what the harness changes after one turn rests and before the next.

An exploratory conversation often needs the world to move between two things the person
says: a file edited from outside, a sibling service stopped, a pause for something to
settle. Each kind of step is held here in a whole scenario, through the real runner and
`HttpHub`, against `FakeLucy` and `FakeShell`. So is the rule that makes them safe to lean
on: a step that does not end as written leaves its turn unsent and the scenario an `error`,
because the conversation after it would not be the one written down. Nothing sleeps and no
process starts: the clock only moves when the harness waits or a fake command takes time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eval_fakes import Clock, Ends, FakeLucy, FakeShell, Play

from lucy_api.evals.conversation import Conversation, Pace, describe_before
from lucy_api.evals.host import NOT_ALLOWED
from lucy_api.evals.loader import parse_scenario
from lucy_api.evals.results import ERROR, PASSED, BeforeRecord, ScenarioRecord
from lucy_api.evals.runner import Plan, Runner
from lucy_api.evals.scenario import TurnSpec, Wait

if TYPE_CHECKING:
    from lucy_api.evals.results import TurnRecord
    from lucy_api.evals.runner import Job

MODEL = "clyde:haiku"
INPUTS = ("POST", "/v1/sessions/ses_1/inputs")

EDITED = """\
summary = "A file edited from outside between two turns is there for the second."

[[turns]]
say = "Keep an eye on review.md for me."

[[turns]]
say = "Anything happen to review.md?"

[[turns.before]]
op = "workspace.write"
input = { path = "review.md", content = "edited from outside\\n", mode = "append" }

[[turns.before]]
wait_seconds = 5
"""


class Watching:
    """An observer that keeps what a person watching the run would be told."""

    def __init__(self) -> None:
        self.lines: list[tuple[int, str]] = []
        self.rested: list[int] = []

    def started(self, job: Job, number: int, total: int) -> None:
        """Nothing to keep."""

    def turn_finished(self, job: Job, turn: TurnRecord) -> None:
        self.rested.append(turn.index)

    def turn_event(self, job: Job, index: int, line: str) -> None:
        self.lines.append((index, line))

    def finished(self, record: ScenarioRecord) -> None:
        """Nothing to keep."""


class Held:
    """One scenario held against a fake hub, and what a test wants to look at afterwards.

    Its commands end as ``endings`` says, through a fake shell on the same clock. Held with
    ``allowed=False``, the runner is given no shell at all, as a run without
    ``--allow-host`` is.
    """

    def __init__(
        self,
        fake: FakeLucy,
        text: str,
        *,
        endings: dict[str, Ends] | None = None,
        allowed: bool = True,
    ) -> None:
        self.clock = Clock()
        self.shell = FakeShell(self.clock)
        self.shell.endings.update(endings or {})
        self.watching = Watching()
        scenario = parse_scenario(
            text.encode(), name="sample", suite="tests", path="tests/sample.toml"
        )
        runner = Runner(
            fake.hub(),
            pace=Pace(clock=self.clock, sleep=self.clock.sleep),
            observer=self.watching,
            shell=self.shell if allowed else None,
        )
        records: list[ScenarioRecord] = []
        runner.run(Plan(scenarios=(scenario,), models=(MODEL,)), records.append)
        self.record = records[0]


def calls(fake: FakeLucy) -> list[tuple[str, str]]:
    return [(request.method, request.url.path) for request in fake.requests]


# --------------------------------------------------------------------------------------
# Steps that end as written
# --------------------------------------------------------------------------------------


def test_a_file_edited_between_two_turns_is_edited_after_the_first_rests() -> None:
    fake = FakeLucy()
    fake.files["review.md"] = "# Review\n"
    fake.say("Anything happen to review.md?", Play(reply="Yes: somebody added a line."))

    held = Held(fake, EDITED)

    record = held.record
    assert record.outcome == PASSED
    assert record.reason == ""
    assert fake.files["review.md"] == "# Review\nedited from outside\n"
    first, second = record.turns
    assert first.before == ()
    assert second.before == (
        BeforeRecord(
            kind="op",
            step="workspace.write",
            status="ok",
            passed=True,
            input={"path": "review.md", "content": "edited from outside\n", "mode": "append"},
            output='{"path": "review.md", "written": true}',
        ),
        BeforeRecord(kind="wait_seconds", step="5s", status="ok", passed=True, seconds=5.0),
    )
    seen = calls(fake)
    wrote = seen.index(("POST", "/v1/tools/workspace.write/invoke"))
    first_rested = max(i for i, call in enumerate(seen) if call == ("GET", "/v1/turns/trn_1"))
    said = [i for i, call in enumerate(seen) if call == INPUTS]
    assert first_rested < wrote < said[1]
    assert held.clock.slept == [1.0, 5.0, 1.0]
    assert second.seconds == 1.0, "the turn's own time does not include the steps before it"


def test_each_step_is_heard_as_it_ends_and_before_the_turn_it_belongs_to() -> None:
    fake = FakeLucy()
    fake.say("Anything happen to review.md?", Play(ran=(("workspace.read", "ok"),)))
    held = Held(fake, EDITED)
    assert held.watching.lines == [
        (2, "> workspace.write -> ok"),
        (2, "> waited 5s"),
        (2, "· workspace.read -> ok"),
    ]
    assert held.watching.rested == [1, 2]


def test_a_step_before_the_first_turn_runs_after_the_seeds_and_before_anything_is_said() -> None:
    fake = FakeLucy()
    text = (
        'summary = "Planted, then touched."\n'
        '[[seed]]\nop = "workspace.write"\ninput = { path = "a.md", content = "one\\n" }\n'
        '[[turns]]\nsay = "Hello?"\n'
        '[[turns.before]]\nop = "workspace.write"\n'
        'input = { path = "a.md", content = "two\\n", mode = "append" }\n'
    )
    held = Held(fake, text)
    assert held.record.outcome == PASSED
    assert fake.files["a.md"] == "one\ntwo\n"
    seen = calls(fake)
    writes = [
        i for i, call in enumerate(seen) if call == ("POST", "/v1/tools/workspace.write/invoke")
    ]
    assert max(writes) < seen.index(INPUTS)
    assert [record.step for record in held.record.turns[0].before] == ["workspace.write"]


def test_a_step_can_prove_something_is_not_there() -> None:
    fake = FakeLucy()
    text = (
        'summary = "Absent."\n[[turns]]\nsay = "Hello?"\n'
        '[[turns.before]]\nop = "workspace.read"\ninput = { path = "gone.md" }\nstatus = "error"\n'
    )
    held = Held(fake, text)
    assert held.record.outcome == PASSED
    [read] = held.record.turns[0].before
    assert (read.status, read.passed, read.error) == ("error", True, "no file gone.md")
    assert held.watching.lines == [(1, "> workspace.read -> error")]


# --------------------------------------------------------------------------------------
# A step that does not end as written
# --------------------------------------------------------------------------------------


def test_a_step_that_fails_leaves_its_turn_unsent_and_the_scenario_an_error() -> None:
    fake = FakeLucy()
    text = (
        'summary = "Three turns."\n'
        '[[turns]]\nsay = "Hello?"\n'
        '[[turns]]\nsay = "And now?"\n'
        '[[turns.before]]\nop = "workspace.read"\ninput = { path = "missing.md" }\n'
        "[[turns.before]]\nwait_seconds = 5\n"
        '[[turns]]\nsay = "And after that?"\n'
    )
    held = Held(fake, text)

    record = held.record
    why = "before 1 workspace.read: status is ok (error: no file missing.md)"
    assert record.outcome == ERROR
    assert record.reason == f"turn 2 was not sent: {why}; 1 later turn(s) were not sent either"
    first, second = record.turns
    assert first.unsent == ""
    assert second.unsent == why
    assert (second.said, second.turn_id, second.status, second.checks) == ("And now?", "", "", ())
    assert second.before == (
        BeforeRecord(
            kind="op",
            step="workspace.read",
            status="error",
            passed=False,
            input={"path": "missing.md"},
            error="no file missing.md",
        ),
    )
    assert list(fake.turns) == ["trn_1"]
    assert calls(fake).count(INPUTS) == 1
    assert held.clock.slept == [1.0], "the wait after the failing step was never taken"
    assert held.watching.rested == [1]
    assert held.watching.lines == [(2, "> workspace.read -> error")]
    assert fake.archived == ["ses_1"]


def test_an_operation_that_runs_but_says_the_wrong_thing_stops_the_turn_too() -> None:
    fake = FakeLucy()
    text = (
        'summary = "Wrong words."\n[[turns]]\nsay = "Hello?"\n'
        '[[turns.before]]\nop = "notes.search"\noutput_avoids = ["plain"]\n'
    )
    held = Held(fake, text)
    record = held.record
    assert record.outcome == ERROR
    assert record.reason.startswith(
        'turn 1 was not sent: before 1 notes.search: output avoids /plain/ ("plain text"'
    )
    [search] = record.turns[0].before
    assert (search.status, search.passed, search.output) == ("ok", False, "plain text")
    assert fake.turns == {}


def test_a_step_is_described_by_what_it_did() -> None:
    wrote = BeforeRecord(kind="op", step="workspace.write", status="refused", passed=False)
    assert describe_before(wrote) == "> workspace.write -> refused"
    waited = BeforeRecord(kind="wait_seconds", step="2.5s", status="ok", passed=True)
    assert describe_before(waited) == "> waited 2.5s"


def test_a_conversation_nobody_is_listening_to_still_takes_its_steps() -> None:
    fake = FakeLucy()
    clock = Clock()
    hub = fake.hub()
    conversation = Conversation(
        hub, hub.create_session({"model": MODEL}), pace=Pace(clock=clock, sleep=clock.sleep)
    )
    turn = conversation.take_turn(1, TurnSpec(say="Hello?", before=(Wait(2.0),)), timeout=30.0)
    assert turn.status == "completed"
    assert [(record.kind, record.step) for record in turn.before] == [("wait_seconds", "2s")]
    assert clock.slept == [2.0, 1.0]


# --------------------------------------------------------------------------------------
# Commands on this machine
# --------------------------------------------------------------------------------------

STOP = "docker stop lucy-family-memory-1"
START = "docker start lucy-family-memory-1"
OUTAGE = f"""\
summary = "Memory goes down between two turns, and comes back before the third."

[[turns]]
say = "Remember that I prefer tea."

[[turns]]
say = "What do I drink?"

[[turns.before]]
host = "{STOP}"
timeout_seconds = 60

[[turns]]
say = "And now?"

[[turns.before]]
host = "{START}"
"""


def test_a_command_runs_on_this_machine_between_two_turns() -> None:
    fake = FakeLucy()
    stopped = Ends(output="lucy-family-memory-1\n", seconds=10.4)
    held = Held(fake, OUTAGE, endings={STOP: stopped})

    record = held.record
    assert record.outcome == PASSED
    assert held.shell.ran == [(STOP, 60.0), (START, 120.0)]
    assert record.turns[1].before == (
        BeforeRecord(
            kind="host",
            step=STOP,
            status="ok",
            passed=True,
            seconds=10.4,
            output="lucy-family-memory-1\n",
        ),
    )
    assert held.watching.lines == [
        (2, f"> $ {STOP} -> ok in 10.4s"),
        (3, f"> $ {START} -> ok in 0.0s"),
    ]
    assert record.turns[1].seconds == 1.0, "the turn's own time does not include the command"


def test_a_command_that_fails_leaves_its_turn_unsent_with_its_exit_status_and_output() -> None:
    fake = FakeLucy()
    said = "Error response from daemon: No such container: lucy-family-memory-1\n"
    held = Held(fake, OUTAGE, endings={STOP: Ends(exit_code=1, output=said, seconds=0.3)})

    record = held.record
    why = f"before 1 `{STOP}`: exited 1: {said.strip()}"
    assert record.outcome == ERROR
    assert record.reason == f"turn 2 was not sent: {why}; 1 later turn(s) were not sent either"
    first, second = record.turns
    assert (first.unsent, second.unsent) == ("", why)
    assert second.before == (
        BeforeRecord(
            kind="host", step=STOP, status="exit 1", passed=False, seconds=0.3, output=said
        ),
    )
    assert held.shell.ran == [(STOP, 60.0)], "the start before turn 3 never ran"
    assert list(fake.turns) == ["trn_1"]
    assert held.watching.lines == [(2, f"> $ {STOP} -> exit 1 in 0.3s")]


def test_a_command_still_running_at_its_timeout_leaves_its_turn_unsent() -> None:
    fake = FakeLucy()
    held = Held(fake, OUTAGE, endings={STOP: Ends(exit_code=None, output="Stopping...\n")})

    record = held.record
    why = f"before 1 `{STOP}`: timed out after 60s: Stopping..."
    assert record.outcome == ERROR
    assert record.turns[1].unsent == why
    [stop] = record.turns[1].before
    assert (stop.status, stop.passed, stop.seconds) == ("timed out", False, 60.0)
    assert held.watching.lines == [(2, f"> $ {STOP} -> timed out in 60.0s")]


def test_a_runner_given_no_shell_refuses_every_command() -> None:
    fake = FakeLucy()
    held = Held(fake, OUTAGE, allowed=False)

    record = held.record
    why = f"before 1 `{STOP}`: {NOT_ALLOWED}"
    assert record.outcome == ERROR
    assert record.reason == f"turn 2 was not sent: {why}; 1 later turn(s) were not sent either"
    assert record.turns[1].before == (
        BeforeRecord(kind="host", step=STOP, status="refused", passed=False, error=NOT_ALLOWED),
    )
    assert held.shell.ran == []
    assert held.watching.lines == [(2, f"> $ {STOP} -> refused in 0.0s")]
