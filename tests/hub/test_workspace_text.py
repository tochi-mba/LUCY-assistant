"""Fingerprints, numbered windows, and the edit ladder — no sandbox required."""

from __future__ import annotations

from lucy_api.workspace.text import (
    DEFAULT_LINE_LIMIT,
    EXACT,
    FUZZY,
    WHITESPACE,
    apply_edit,
    digest,
    locate,
    numbered_window,
    stale_if_changed,
    validate_text,
)


def test_a_read_window_numbers_lines_and_confesses_the_count() -> None:
    window = numbered_window("alpha\nbeta\ngamma\n", start_line=2, limit=2)

    assert window.numbered == "2\tbeta\n3\tgamma"
    assert "showing lines 2-3 of 3" in window.notice
    assert window.file_digest == digest("alpha\nbeta\ngamma\n")
    assert window.window_digest == digest("beta\ngamma")


def test_a_start_line_past_the_end_shows_nothing_and_says_so() -> None:
    window = numbered_window("only\n", start_line=9, limit=DEFAULT_LINE_LIMIT)

    assert window.numbered == ""
    assert "showing 0 of 1 lines" in window.notice
    assert "past the end" in window.notice


def test_an_empty_file_is_showing_zero_of_zero() -> None:
    window = numbered_window("", truncated=True)

    assert "showing 0 of 0 lines" in window.notice
    assert "retrieved prefix" in window.notice


def test_a_long_line_is_clipped_with_an_omission_count() -> None:
    window = numbered_window("x" * 2_050)

    assert "characters omitted" in window.numbered
    assert window.numbered.startswith("1\t")


def test_an_exact_unique_edit_replaces_once() -> None:
    applied = apply_edit("keep one keep", "one", "two")

    assert applied.replaced
    assert applied.text == "keep two keep"
    assert applied.match is not None
    assert applied.match.rung == EXACT


def test_an_ambiguous_exact_match_names_every_line() -> None:
    applied = apply_edit("one\nmid\none\n", "one", "two")

    assert not applied.replaced
    assert "lines: 1, 3" in applied.notice


def test_whitespace_normalised_match_replaces_the_original_slice() -> None:
    haystack = "def f():\n    return 1\n"
    needle = "def f():\n  return 1"
    applied = apply_edit(haystack, needle, "def f():\n    return 2\n")

    assert applied.replaced
    assert applied.match is not None
    assert applied.match.rung == WHITESPACE
    assert "return 2" in applied.text


def test_ambiguous_whitespace_matches_are_refused() -> None:
    haystack = "def f():\n    x\ndef f():\n    x\n"
    applied = apply_edit(haystack, "def f():\n  x", "pass")

    assert not applied.replaced
    assert "Multiple occurrences" in applied.notice


def test_line_ending_differences_are_absorbed_by_whitespace_normalisation() -> None:
    haystack = "alpha\r\nbeta\r\n"
    applied = apply_edit(haystack, "alpha\nbeta", "gamma")

    assert applied.replaced
    assert applied.match is not None
    assert applied.match.rung == WHITESPACE


def test_a_fuzzy_match_uses_first_and_last_non_blank_anchors() -> None:
    haystack = "start unique\nmiddle drifted a little\nend unique\n"
    needle = "start unique\nmiddle is different\nend unique"
    applied = apply_edit(haystack, needle, "replaced\n")

    assert applied.replaced
    assert applied.match is not None
    assert applied.match.rung == FUZZY
    assert applied.text.startswith("replaced")


def test_ambiguous_fuzzy_windows_are_refused() -> None:
    haystack = "head\nfirst body\ntail\nhead\nsecond body\ntail\n"
    needle = "head\nunknown body\ntail"
    applied = apply_edit(haystack, needle, "x")

    assert not applied.replaced
    assert "Multiple occurrences" in applied.notice or "not found" in applied.notice


def test_a_miss_shows_the_nearest_window_as_a_diff() -> None:
    applied = apply_edit("alpha\nbeta\n", "alpa\nbeta", "gamma")

    assert not applied.replaced
    assert "not found" in applied.notice
    assert "old_str" in applied.notice or "---" in applied.notice or applied.located.nearest


def test_fuzzy_skips_windows_that_are_too_different_or_missing_an_anchor() -> None:
    missing_end = apply_edit("start unique\nmiddle only\n", "start unique\n??\nend unique", "x")
    assert not missing_end.replaced
    bloated = "start unique\n" + ("z" * 400) + "\nend unique\n"
    applied = apply_edit(bloated, "start unique\ny\nend unique", "x")
    assert not applied.replaced
    applied = apply_edit("", "missing", "x")

    assert not applied.replaced
    assert applied.located.nearest == ""


def test_an_empty_old_string_is_refused_before_searching() -> None:
    applied = apply_edit("body", "", "x")

    assert not applied.replaced
    assert "empty" in applied.notice


def test_a_stale_fingerprint_names_the_reread() -> None:
    text = "current"
    assert stale_if_changed(text, "") == ""
    assert stale_if_changed(text, digest(text)) == ""
    assert "changed since you read it" in stale_if_changed(text, "deadbeefdeadbeef")


def test_json_toml_and_python_are_validated_and_other_suffixes_are_not() -> None:
    assert validate_text("ok.json", '{"a": 1}') == ""
    assert "JSON" in validate_text("bad.json", "{")
    assert validate_text("ok.toml", "a = 1\n") == ""
    assert "TOML" in validate_text("bad.toml", "[[[")
    assert validate_text("ok.py", "x = 1\n") == ""
    assert "Python" in validate_text("bad.py", "def (\n")
    assert validate_text("notes.md", "not code") == ""


def test_a_newline_only_needle_that_does_not_occur_falls_through_the_ladder() -> None:
    located = locate("body", "\n")

    assert located.matches == ()
    assert located.unique is None


def test_a_fuzzy_window_below_the_similarity_floor_is_skipped() -> None:
    haystack = "start unique\n" + ("zzzz\n" * 40) + "end unique\n"
    needle = "start unique\nhello world this is unrelated prose\nend unique"
    applied = apply_edit(haystack, needle, "x")

    assert not applied.replaced


def test_a_distant_miss_has_no_nearest_diff() -> None:
    applied = apply_edit("zzzzzzzz", "alpha beta gamma delta epsilon", "x")

    assert not applied.replaced
    assert applied.located.nearest == ""


def test_whitespace_only_old_string_does_not_invent_a_fuzzy_match() -> None:
    located = locate("visible\n", "   \n   ")

    assert located.unique is None or located.rung != FUZZY
    assert locate("body", "").matches == ()
    from lucy_api.workspace.text import WHITESPACE, _fold_indent, _folded_windows

    # splitlines() on a lone newline is [''], not []; only the empty string yields no lines.
    assert _folded_windows("body", "", _fold_indent, WHITESPACE) == ()
