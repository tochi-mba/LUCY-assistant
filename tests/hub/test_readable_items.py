"""The model reads a transcript item as what happened, not as the row it is stored in.

Read in the requests the hub actually sent: an approval reached the model as JSON in the
person's voice -- an opaque approval id, `is_automatic: false`, one sentence twice as
`description` and `reason` -- and every tool result carried `duration_ms: 183.080810546875`, an
empty `note` and an empty `notices`.
"""

from __future__ import annotations

import pytest

from lucy_api.turn.prompt import items_from_rows
from lucy_api.turn.readable import readable

ASKED = {
    "approval_id": "apr_gHKbejBdJzKUNIeeCQitc6jkIwGaRsmL",
    "tool": "notes.setFact",
    "description": "Remember and change notes about you needs approval before it can run.",
    "arguments": {"title": "Weekly review", "body": "Friday afternoons"},
    "reason": "Remember and change notes about you needs approval before it can run.",
    "policy": "ask",
    "is_automatic": False,
    "permission": "notes.write",
}
RESULT = {
    "step_id": "keep",
    "operation": "notes.setFact",
    "status": "ok",
    "note": "",
    "summary": '<result source="notes" trust="reported">...</result>',
    "notices": [],
    "error": "",
    "duration_ms": 183.080810546875,
}


def test_an_approval_request_reads_as_what_was_asked() -> None:
    """The bug, named: this reached the model as the JSON above."""
    text = readable("approval_request", ASKED)
    assert text == (
        '[asked the person to approve notes.setFact(title="Weekly review", body="Friday '
        'afternoons"): Remember and change notes about you needs approval before it can run.]'
    )
    assert "apr_" not in text
    assert "is_automatic" not in text


@pytest.mark.parametrize(
    ("answer", "text"),
    [
        ({"approved": True, "lifetime": "once"}, "[the person approved it, this once]"),
        ({"approved": True, "lifetime": "session"}, "[the person approved it, for this session]"),
        (
            {"approved": False, "instruction": " Call it weekly.md "},
            "[the person declined it: Call it weekly.md]",
        ),
        ({"approved": False, "instruction": ""}, "[the person declined it]"),
    ],
)
def test_an_answer_reads_as_what_the_person_said(answer: dict[str, object], text: str) -> None:
    assert readable("approval_response", answer) == text


def test_a_tool_result_reads_as_its_step_and_result_without_timing_or_empty_fields() -> None:
    assert readable("tool_result", RESULT) == (
        '[step keep: notes.setFact -- ok]\n<result source="notes" trust="reported">...</result>'
    )


def test_a_failed_step_reads_as_its_purpose_notices_and_error() -> None:
    failed = {
        **RESULT,
        "status": "error",
        "note": "Keep the review day.",
        "notices": ["retried once"],
        "error": "the notes capability could not be reached",
        "summary": "",
    }
    assert readable("tool_result", failed) == (
        "[step keep: notes.setFact -- error]\n"
        "for: Keep the review day.\n"
        "notice: retried once\n"
        "error: the notes capability could not be reached"
    )


def test_an_error_reads_as_its_code_and_detail() -> None:
    assert readable("error", {"code": "empty_reply", "detail": "nothing was said"}) == (
        "[error empty_reply: nothing was said]"
    )


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        ("tool_result", {"op": "notes.search"}),
        ("approval_request", {"approval_id": "apr_1"}),
        ("reasoning", {"text": "thinking"}),
        ("tool_result", ["not", "a", "dict"]),
    ],
)
def test_anything_of_another_shape_is_still_passed_whole(kind: str, content: object) -> None:
    """A writer that adds a shape this module does not know loses nothing."""
    assert readable(kind, content).startswith(("{", "["))


def test_text_is_passed_as_it_is() -> None:
    assert readable("message", "hello") == "hello"


def test_the_prompt_reads_items_through_it() -> None:
    [item] = items_from_rows(
        [
            {
                "id": "itm_1",
                "seq": 1,
                "role": "assistant",
                "content": ASKED,
                "type": "approval_request",
            }
        ]
    )
    assert item.body.startswith("[asked the person to approve notes.setFact(")
