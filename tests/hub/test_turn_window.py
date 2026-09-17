"""Focusing a spilled result on a unique snippet, then spilling that window if it is still huge."""

from __future__ import annotations

from lucy_api.turn.window import (
    MAX_OCCURRENCES,
    QUOTE_LIMIT,
    UNSHOWN,
    allow_show_from,
    attach_needles,
    focus,
    needle_from,
    spill,
    window,
    without_needles,
)


def test_a_blank_fingerprint_leaves_the_body_alone() -> None:
    body = "head middle tail"
    assert focus(body, "").text == body
    assert focus(body, "   ").text == body
    assert focus(body, "\n").notices == ()


def test_a_unique_fingerprint_starts_display_at_the_match() -> None:
    viewed = focus("alpha\nERROR: timeout\nbeta", "ERROR: timeout")
    assert viewed.shown is True
    assert viewed.text.startswith("ERROR: timeout")
    assert "alpha" not in viewed.text
    assert "line 2 character 1" in viewed.notices[0]


def test_a_missing_fingerprint_does_not_dump_the_payload() -> None:
    payload = "secret-payload " * 20
    viewed = focus(payload, "no such snippet")
    assert viewed.shown is False
    assert viewed.text == UNSHOWN
    assert payload not in viewed.text
    assert payload not in viewed.notices[0]
    assert "was not found" in viewed.notices[0]


def test_a_repeated_fingerprint_lists_the_lines_and_hides_the_body() -> None:
    body = "one\nMATCH here\nMATCH there"
    viewed = focus(body, "MATCH")
    assert viewed.shown is False
    assert viewed.text == UNSHOWN
    assert "matched 2 times" in viewed.notices[0]
    assert "line 2 character 1" in viewed.notices[0]
    assert "line 3 character 1" in viewed.notices[0]
    assert "MATCH here" not in viewed.notices[0]


def test_overlapping_matches_count_as_collisions() -> None:
    viewed = focus("aaaa", "aa")
    assert viewed.shown is False
    assert "matched 3 times" in viewed.notices[0]


def test_a_still_oversized_window_keeps_the_head_and_tail_of_the_suffix() -> None:
    body = "head-" + ("x" * 80) + "MID-" + ("y" * 80) + "-tail"
    viewed = window(body, "MID-", cap=10)
    assert viewed.text.startswith("MID-")
    assert viewed.text.endswith("-tail")
    assert "head-" not in viewed.text
    assert any("spilled" in notice for notice in viewed.notices)
    assert any("unique fingerprint" in notice for notice in viewed.notices)


def test_a_small_suffix_is_not_spilled() -> None:
    viewed = window("prefix NEEDLE rest", "NEEDLE", cap=10_000)
    assert viewed.text == "NEEDLE rest"
    assert not any("spilled" in notice for notice in viewed.notices)


def test_spill_under_the_cap_is_a_no_op() -> None:
    kept, notices = spill("short", cap=100)
    assert kept == "short"
    assert notices == ()


def test_a_failed_focus_does_not_then_spill_the_placeholder() -> None:
    viewed = window("payload", "missing", cap=1)
    assert viewed.text == UNSHOWN
    assert not any("spilled" in notice for notice in viewed.notices)


def test_needle_from_prefers_the_step_over_the_input() -> None:
    raw = {
        "show_from": "from-step",
        "fingerprint": "ignored",
        "input": {"show_from": "from-input", "fingerprint": "also-ignored"},
    }
    assert needle_from(raw) == "from-step"
    assert needle_from("not a mapping") == ""
    assert needle_from({"input": ["not", "a", "mapping"]}) == ""
    assert needle_from({"input": {"fingerprint": "  from-input  "}}) == "  from-input  "
    assert needle_from({"fingerprint": "   "}) == ""
    assert needle_from({"fingerprint": 12}) == ""


def test_attach_needles_copies_from_the_plan_when_the_result_omitted_it() -> None:
    result = {"steps": [{"id": "hits", "data": "body"}]}
    attach_needles(
        {"steps": [{"id": "hits", "op": "research.search", "show_from": "ERROR:"}]},
        result,
    )
    assert result["steps"][0]["show_from"] == "ERROR:"


def test_attach_needles_does_not_overwrite_a_result_that_already_chose() -> None:
    result = {"steps": [{"id": "hits", "show_from": "kept"}]}
    attach_needles({"steps": [{"id": "hits", "show_from": "plan"}]}, result)
    assert result["steps"][0]["show_from"] == "kept"


def test_attach_needles_ignores_shapeless_payloads() -> None:
    result = {"steps": "not-a-list"}
    attach_needles("plan", result)
    attach_needles({"steps": "also-not"}, {"steps": [{"id": "hits"}]})
    attach_needles({"steps": [None, {"id": ""}]}, {"steps": [None, {"id": "hits"}]})
    attach_needles({"steps": [{"id": "hits", "show_from": "x"}]}, {"steps": "nope"})
    attach_needles({"steps": [{"show_from": "x"}]}, {"steps": [{"id": "hits"}]})
    assert result["steps"] == "not-a-list"


def test_a_long_fingerprint_is_quoted_short_in_the_notice() -> None:
    needle = "n" * (QUOTE_LIMIT + 20)
    viewed = focus("zzzz", needle)
    quoted = viewed.notices[0]
    assert needle not in quoted
    assert "…" in quoted


def test_collision_notices_stop_enumerating_after_the_cap() -> None:
    body = "ab" * (MAX_OCCURRENCES + 3)
    viewed = focus(body, "a")
    assert f"matched {MAX_OCCURRENCES + 3} times" in viewed.notices[0]
    assert "and 3 more" in viewed.notices[0]


def test_the_plan_schema_gains_show_from_on_each_step() -> None:
    schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "op": {"type": "string"}},
                },
            }
        },
    }
    patched = allow_show_from(schema)
    step = patched["properties"]["steps"]["items"]["properties"]
    assert "show_from" in step
    assert "id" in schema["properties"]["steps"]["items"]["properties"]
    assert "show_from" not in schema["properties"]["steps"]["items"]["properties"]


def test_an_already_documented_show_from_field_is_left_alone() -> None:
    schema = {
        "properties": {
            "id": {},
            "op": {},
            "show_from": {"type": "string", "description": "already here"},
        }
    }
    patched = allow_show_from(schema)
    assert patched["properties"]["show_from"]["description"] == "already here"


def test_schema_walk_covers_lists_and_scalars() -> None:
    patched = allow_show_from({"anyOf": [{"type": "string"}, 1, {"properties": {}}]})
    assert patched["anyOf"][1] == 1


def test_without_needles_strips_lucy_fields_and_leaves_the_plan_alone() -> None:
    plan = {
        "steps": [
            {
                "id": "hits",
                "op": "research.search",
                "show_from": "ERROR:",
                "fingerprint": "also",
                "input": {"query": "tour", "show_from": "inside"},
            },
            "not-a-step",
            {"id": "other", "op": "research.open", "input": "already-a-string"},
        ]
    }
    cleaned = without_needles(plan)
    assert cleaned["steps"][0]["input"] == {"query": "tour"}
    assert "show_from" not in cleaned["steps"][0]
    assert plan["steps"][0]["show_from"] == "ERROR:"
    assert cleaned["steps"][2]["input"] == "already-a-string"
    assert without_needles("not-a-plan") == "not-a-plan"
    assert without_needles({"steps": None}) == {"steps": []}
