"""The watchdog: which transcripts stop a turn, which do not, and what a watcher reports.

Each rule is pinned both ways: the transcript that must halt, and the nearest one that must
not, because a watchdog that halts too eagerly is switched off by the first person it annoys.
"""

from __future__ import annotations

from typing import Any

from lucy_api.evals.scenario import OpMatch
from lucy_api.evals.transcript import Ask, Exchange, ToolResult, call_key
from lucy_api.evals.watch import (
    ASKED_AGAIN,
    ERROR_ITEM,
    FAILED_AGAIN,
    REPAIRED_AGAIN,
    RULES,
    SHOWN,
    STEP_ERROR,
    Halt,
    Watcher,
    cause,
    describe_ask,
    describe_result,
    watch,
)

PLAY = 'music.play:{"uri":"$find_track"}'
REFUSED = "Step 'approved_1' failed while running 'music.play': music answered 422"


def op(source: str) -> OpMatch:
    return OpMatch(source=source, alternatives=tuple(source.split("|")))


def failed(operation: str, error: str = REFUSED) -> ToolResult:
    return ToolResult(operation, "error", error=error)


def exchange(**parts: Any) -> Exchange:
    return Exchange(said="Play it.", reply="", **parts)


def item(kind: str, content: Any, *, turn: str = "trn_1", number: int = 0) -> dict[str, Any]:
    return {"id": f"itm_{number}", "turn_id": turn, "type": kind, "role": "x", "content": content}


# --- nothing wrong ------------------------------------------------------------------------


def test_a_turn_with_nothing_wrong_is_not_halted() -> None:
    quiet = exchange(
        results=(
            ToolResult("notes.search", "ok"),
            ToolResult("workspace.delete", "denied"),
            ToolResult("workspace.write", "skipped"),
        ),
        asks=(Ask("apr_1", "workspace.delete", answer="denied", call="workspace.delete:{}"),),
    )
    assert watch(quiet) is None
    assert RULES == (STEP_ERROR, ERROR_ITEM, FAILED_AGAIN, ASKED_AGAIN, REPAIRED_AGAIN)


# --- step-error ---------------------------------------------------------------------------


def test_a_failed_step_halts_with_its_cause_and_not_the_executor_s_step_name() -> None:
    halt = watch(exchange(results=(failed("music.play"),)))
    assert halt == Halt(STEP_ERROR, "music.play failed: music answered 422")
    assert str(halt) == "step-error: music.play failed: music answered 422"


def test_a_failure_the_turn_allows_is_not_halted() -> None:
    results = (failed("workspace.read", "no such file"),)
    assert watch(exchange(results=results), allowed=(op("workspace.read|workspace.list"),)) is None
    assert watch(exchange(results=results), allowed=(op("workspace.*"),)) is None
    halt = watch(exchange(results=results), allowed=(op("workspace.list"),))
    assert halt is not None
    assert halt.rule == STEP_ERROR


def test_a_failure_with_no_error_text_still_says_something() -> None:
    assert cause(ToolResult("x.y", "error")) == "no error text"
    assert watch(exchange(results=(ToolResult("x.y", "error"),))) == Halt(
        STEP_ERROR, "x.y failed: no error text"
    )


# --- failed-again -------------------------------------------------------------------------


def test_the_same_allowed_failure_twice_is_halted() -> None:
    results = (failed("workspace.read", "no such file"), failed("workspace.read", "no such file"))
    halt = watch(exchange(results=results), allowed=(op("workspace.read"),))
    assert halt == Halt(FAILED_AGAIN, "workspace.read failed the same way twice: no such file")


def test_the_same_failure_under_two_step_names_is_the_same_failure() -> None:
    renamed = "Step 'approved_2' failed while running 'music.play': music answered 422"
    halt = watch(
        exchange(results=(failed("music.play"), failed("music.play", renamed))),
        allowed=(op("music.play"),),
    )
    assert halt is not None
    assert halt.rule == FAILED_AGAIN


def test_two_different_failures_of_an_allowed_operation_are_not_halted() -> None:
    results = (failed("workspace.read", "no such file"), failed("workspace.read", "is a folder"))
    assert watch(exchange(results=results), allowed=(op("workspace.read"),)) is None


# --- error-item ---------------------------------------------------------------------------


def test_an_error_the_hub_wrote_into_the_transcript_halts() -> None:
    assert watch(exchange(errors=("model_error: the model stopped",))) == Halt(
        ERROR_ITEM, "model_error: the model stopped"
    )


SENT_BACK = (
    "invalid_plan: The previous plan was invalid: Step 'cancel_counter': input.work_id holds "
    "the reference '$spawn_counter', but this field does not take one"
)


def test_a_plan_sent_back_to_the_model_to_repair_is_not_a_halt() -> None:
    """The bug, named: asked to start a helper and stop it at once, the model put the spawn's
    handle where `work.cancel` takes plain text. The hub sent the plan back to be repaired --
    the loop working as designed -- and the watchdog halted the turn on that notice, so the
    repair never happened and four later turns were never said."""
    assert watch(exchange(errors=(SENT_BACK,))) is None
    assert watch(exchange(errors=(SENT_BACK, "invalid_plan: another plan, another fix"))) is None


def test_the_same_plan_sent_back_twice_halts() -> None:
    halt = watch(exchange(errors=(SENT_BACK, SENT_BACK)))
    assert halt is not None
    assert halt.rule == REPAIRED_AGAIN
    assert halt.detail.startswith("the same plan was sent back twice: invalid_plan:")


def test_a_real_error_after_a_repair_notice_still_halts() -> None:
    assert watch(exchange(errors=(SENT_BACK, "model_error: the model stopped"))) == Halt(
        ERROR_ITEM, "model_error: the model stopped"
    )


# --- a turn expected to fail --------------------------------------------------------------


def test_a_turn_expected_to_fail_is_not_halted_for_failing_but_is_for_repeating() -> None:
    failing = exchange(results=(failed("notes.search"),), errors=("empty_reply",))
    assert watch(failing, expecting_failure=True) is None
    repeating = exchange(results=(failed("notes.search"), failed("notes.search")))
    halt = watch(repeating, expecting_failure=True)
    assert halt is not None
    assert halt.rule == FAILED_AGAIN


# --- asked-again --------------------------------------------------------------------------


def test_an_ask_for_a_call_already_answered_halts() -> None:
    asks = (
        Ask("apr_1", "music.play", arguments="uri=$find_track", answer="approved", call=PLAY),
        Ask("apr_2", "music.play", arguments="uri=$find_track", call=PLAY),
    )
    assert watch(exchange(asks=asks)) == Halt(
        ASKED_AGAIN, "music.play was asked for again after it had been answered (uri=$find_track)"
    )


def test_an_ask_repeated_after_a_refusal_halts_too() -> None:
    asks = (
        Ask("apr_1", "workspace.delete", answer="denied", call="workspace.delete:{}"),
        Ask("apr_2", "workspace.delete", call="workspace.delete:{}"),
    )
    assert watch(exchange(asks=asks)) == Halt(
        ASKED_AGAIN, "workspace.delete was asked for again after it had been answered"
    )


def test_a_different_call_or_an_unanswered_duplicate_is_not_asked_again() -> None:
    other = 'music.play:{"uri":"example:track:2"}'
    different = (
        Ask("apr_1", "music.play", answer="approved", call=PLAY),
        Ask("apr_2", "music.play", call=other),
    )
    assert watch(exchange(asks=different)) is None
    both_waiting = (Ask("apr_1", "music.play", call=PLAY), Ask("apr_2", "music.play", call=PLAY))
    assert watch(exchange(asks=both_waiting)) is None


def test_a_call_is_the_same_call_whatever_order_its_arguments_were_written_in() -> None:
    assert call_key("notes.setFact", {"title": "a", "body": "b"}) == call_key(
        "notes.setFact", {"body": "b", "title": "a"}
    )
    assert call_key("notes.setFact", {"title": "a"}) != call_key("notes.setFact", {"title": "b"})


# --- what is reported ---------------------------------------------------------------------


def test_progress_lines_say_what_ran_and_what_was_asked() -> None:
    assert describe_result(ToolResult("notes.search", "ok")) == "· notes.search -> ok"
    assert describe_result(failed("music.play")) == ("· music.play -> error: music answered 422")
    assert describe_ask(Ask("apr_1", "music.play", arguments="uri=x")) == (
        "? music.play asks to run (uri=x)"
    )
    assert describe_ask(Ask("apr_1", "workspace.run")) == "? workspace.run asks to run"


def test_a_long_error_is_shortened_on_one_line() -> None:
    line = describe_result(failed("research.open", "word\n" * 200))
    shown = line.removeprefix("· research.open -> error: ")
    assert len(shown) == SHOWN
    assert shown.endswith("…")
    assert "\n" not in shown


def test_a_watcher_reports_each_step_and_ask_once_and_says_when_to_stop() -> None:
    heard: list[str] = []
    watcher = Watcher("trn_1", on_event=heard.append)
    ask = {"approval_id": "apr_1", "tool": "music.play", "arguments": {"uri": "$find_track"}}
    items = [
        item("message", "Play it.", number=1),
        item("approval_request", ask, number=2),
        item("tool_result", {"operation": "music.find", "status": "ok"}, number=3),
        item("tool_result", {"operation": "music.find", "status": "ok"}, turn="trn_2", number=4),
    ]

    _, halt = watcher.look(items)
    assert halt is None
    assert heard == ["· music.find -> ok", "? music.play asks to run (uri=$find_track)"]

    items.append(item("approval_response", {"approval_id": "apr_1", "approved": True}, number=5))
    items.append(item("approval_request", {**ask, "approval_id": "apr_2"}, number=6))
    seen, halt = watcher.look(items)
    assert heard[2:] == ["? music.play asks to run (uri=$find_track)"]
    assert len(seen.asks) == 2
    assert halt is not None
    assert halt.rule == ASKED_AGAIN


def test_a_watcher_with_nobody_listening_still_decides() -> None:
    watcher = Watcher("trn_1", allowed=(op("music.play"),), expecting_failure=False)
    result = {"operation": "music.play", "status": "error", "error": REFUSED}
    _, halt = watcher.look([item("tool_result", result), item("tool_result", result)])
    assert halt is not None
    assert halt.rule == FAILED_AGAIN
