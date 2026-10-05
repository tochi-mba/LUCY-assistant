"""What a compaction summary says, and whose words it says they were.

The bugs, named: compaction read helpers' items as well as the main thread's, so a helper's
brief -- a user-role message in its own transcript -- was summarised as the person saying
"You are read-only. Do not write files"; an approval's JSON was "User:" too; it kept the eight
oldest requests and dropped the rest without a count; every URL of every search went into an
uncapped list the projection never trims; and it opened with "MUST-PRESERVE", an all-caps
order with no object, under a frame claiming the transcript "can be read back", which nothing
can do.
"""

from __future__ import annotations

from typing import Any

from lucy_api.context.projection import _summary_body
from lucy_api.sessions.compact import MAX_IDENTIFIERS, MAX_REQUESTS, _summary


def said(seq: int, text: str, role: str = "user", kind: str = "message") -> dict[str, Any]:
    return {"seq": seq, "content_json": text, "role": role, "type": kind}


def test_only_the_persons_messages_are_their_requests() -> None:
    text = _summary(
        [
            said(1, "Find the tour dates"),
            said(2, '{"approved": true}', kind="approval_response"),
        ],
        covers_to=2,
    )
    assert "The person asked, oldest first: Find the tour dates\n" in text
    assert "approved" not in text


def test_the_first_request_and_the_newest_are_kept_with_a_count() -> None:
    items = [said(n, f"request {n}") for n in range(1, 13)]
    text = _summary(items, covers_to=12)
    first = text.splitlines()[0]
    assert first.startswith(
        f"The person asked, oldest first (the first and the last {MAX_REQUESTS - 1} of 12)"
    )
    assert "request 1 |" in first
    assert "request 2 |" not in first
    assert first.endswith("request 12")


def test_identifiers_are_capped_and_the_ones_people_said_come_first() -> None:
    hits = " ".join(f"https://x.example/{n}" for n in range(60))
    text = _summary(
        [said(1, hits, role="tool", kind="tool_result"), said(2, "look at $plan")],
        covers_to=2,
    )
    line = next(line for line in text.splitlines() if line.startswith("Identifiers seen"))
    assert line.startswith("Identifiers seen: $plan, https://x.example/0")
    assert line.endswith(f"(and {60 + 1 - MAX_IDENTIFIERS} more)")


def test_the_summary_speaks_to_the_model_that_reads_it() -> None:
    text = _summary([said(1, "hello")], covers_to=1)
    assert "MUST-PRESERVE" not in text
    assert "Covered items" not in text


def test_the_frame_says_what_is_gone_and_promises_no_read_back() -> None:
    from lucy_api.context.projection import Compaction

    body = _summary_body(Compaction(seq=1, covers_from=1, covers_to=4, summary="S"), 4)
    assert body.startswith("[harness: 4 earlier entries were replaced by this extract")
    assert "Your replies and the tool results from that part are not shown." in body
    assert "read back" not in body
