"""What declaring a collection buys, and why an opaque result throws it away.

The point of the tool layer is that data moves between steps without becoming tokens. That
only works if the runtime knows a result is a list of things: it needs the shape to label
the entries, to resolve a reference by position, and to answer a question about the result
without fetching it again.
"""

from __future__ import annotations

from typing import Any

from weftai.operation import define_operation
from weftai.schema.ref import ref
from weftai.schema.spec import object_schema, string_schema
from weftai.schema.types import value

from lucy_api.packs.collections import ALL, MAX_LABEL, NOTE, TRACK
from lucy_api.packs.registry import build_registry, build_runtime

NOTES = [
    {"id": "m1", "title": "Tea", "body": "Earl Grey, no sugar", "kind": "fact", "trust": "stated"},
    {"id": "m2", "title": "Coffee", "body": "Only before noon", "kind": "fact", "trust": "stated"},
    {"id": "m3", "title": "Gig", "body": "Went on Tuesday", "kind": "episode", "trust": "observed"},
]


def a_search_operation(returns: list[dict[str, Any]] = NOTES) -> Any:
    async def run(_context: Any) -> list[dict[str, Any]]:
        return returns

    return define_operation(
        {
            "name": "notes.search",
            "description": "Find notes about the person.",
            "input": object_schema({"query": string_schema()}),
            "output": NOTE,
            "effects": "read",
            "run": run,
        }
    )


async def execute(plan: dict[str, Any], operations: list[Any], *, writes: bool = False) -> Any:
    """Writes are off unless a test says otherwise, which is the runtime's own default.

    A step that writes is refused when `allowWrites` is not set, and omitting it is not the
    same as passing `False` by accident -- it is the reason a read-only turn cannot be
    talked into a write.
    """
    runtime = build_runtime(build_registry(operations))
    return await runtime.execute(plan, {"ctx": None, "allowWrites": writes})


# --------------------------------------------------------------------------------------
# The declarations themselves
# --------------------------------------------------------------------------------------


def test_every_collection_has_a_label_and_a_key() -> None:
    """Without both, a result is a blob: nothing to show and nothing to reference."""
    for declared in ALL:
        assert declared.name
        assert declared.label is not None
        assert declared.key is not None


def test_a_label_is_one_line_however_long_the_data_is() -> None:
    """A label is built from text a service returned, and a service can return anything."""
    huge = {"name": "x" * 5_000, "artist": "y" * 5_000}
    label = TRACK.label(huge)

    assert len(label) <= MAX_LABEL
    assert "\n" not in label


def test_a_label_survives_a_record_that_is_missing_fields() -> None:
    assert NOTE.label({}) is not None
    assert NOTE.key({}) == ""


# --------------------------------------------------------------------------------------
# What the runtime does with one
# --------------------------------------------------------------------------------------


async def test_a_collection_comes_back_counted_and_typed() -> None:
    result = await execute(
        {"steps": [{"id": "found", "op": "notes.search", "input": {"query": "tea"}}]},
        [a_search_operation()],
    )
    step = result["steps"][0]

    assert step["kind"] == "collection"
    assert step["type"] == "note"
    assert step["count"] == 3
    assert len(step["items"]) == 3


async def test_the_free_operations_arrive_with_the_collection() -> None:
    """Eight of them, generated because something bound this turn returns notes."""
    names = build_registry([a_search_operation()]).names()

    for suffix in (
        "filter",
        "count",
        "countBy",
        "distinct",
        "mostCommon",
        "first",
        "pick",
        "details",
    ):
        assert f"note.{suffix}" in names


async def test_a_question_about_a_result_costs_no_second_call() -> None:
    """This is the whole argument for collections.

    Asking how many of the notes are facts is arithmetic over something already fetched.
    Without a declared collection the model's only route to that answer is to ask for the
    whole list again and count it in its head -- slower, and wrong more often.
    """
    calls: list[str] = []

    async def run(_context: Any) -> list[dict[str, Any]]:
        calls.append("fetched")
        return NOTES

    operation = define_operation(
        {
            "name": "notes.search",
            "description": "Find notes about the person.",
            "input": object_schema({"query": string_schema()}),
            "output": NOTE,
            "effects": "read",
            "run": run,
        }
    )

    result = await execute(
        {
            "steps": [
                {"id": "found", "op": "notes.search", "input": {"query": "tea"}},
                {
                    "id": "facts",
                    "op": "note.filter",
                    "input": {
                        "from": "$found",
                        "filters": [{"field": "kind", "op": "eq", "value": "fact"}],
                    },
                },
            ]
        },
        [operation],
    )

    assert result["ok"] is True
    assert len(calls) == 1, "the second step read the stored result rather than fetching again"
    assert result["steps"][1]["count"] == 2


async def test_a_later_step_can_name_an_entry_it_was_never_shown() -> None:
    """A reference resolves against the whole result, not against the lines rendered.

    Positions are 1-based, which is what the schema handed to the model says: `$found[3]`
    is the third note. Getting that wrong is an off-by-one in the one place it would be
    hardest to notice, because the wrong entry is still a plausible entry.
    """
    seen: list[Any] = []

    async def play(context: Any) -> dict[str, Any]:
        seen.append(context.input["note"])
        return {"ok": True}

    use = define_operation(
        {
            "name": "notes.confirm",
            "description": "Confirm one note.",
            # `ref()` is what makes a field accept "$found[3]". An ordinary object field
            # refuses the string, which is the right refusal: a reference is only meaningful
            # where the operation declared that it resolves one.
            "input": object_schema({"note": ref(NOTE)}),
            "output": value(object_schema({"ok": string_schema().optional()})),
            "effects": "write",
            "run": play,
        }
    )

    result = await execute(
        {
            "steps": [
                {"id": "found", "op": "notes.search", "input": {"query": "tea"}},
                {"id": "pick", "op": "notes.confirm", "input": {"note": "$found[3]"}},
            ]
        },
        [a_search_operation(), use],
        writes=True,
    )

    assert result["ok"] is True

    # The handler is handed a materialised Collection rather than a bare record, because a
    # reference can select several positions at once. One position is a collection of one.
    resolved = seen[0]
    assert resolved.type == "note"
    assert resolved.count == 1
    assert resolved.items[0]["id"] == "m3", "the third note, which was never rendered"


async def test_nothing_is_generated_for_a_collection_nothing_produces() -> None:
    """Otherwise every turn carries dozens of tools the model cannot use.

    Tool-selection accuracy falls away sharply once a model is choosing among too many, so
    an unusable tool is not free -- it is paid for on every turn, in tokens and in wrong
    choices.
    """
    assert build_registry([]).names() == ()

    only_notes = build_registry([a_search_operation()]).names()
    assert not [name for name in only_notes if name.startswith(("track.", "file.", "topic."))]


async def test_a_write_is_refused_unless_the_turn_allowed_one() -> None:
    """Omitting `allowWrites` is not the same as forgetting it.

    A read-only turn -- the plan mode a person chooses when they want an assistant to look
    but not touch -- depends on this being the default rather than something every caller
    has to remember to pass.
    """

    async def play(_context: Any) -> dict[str, Any]:
        raise AssertionError("a write ran in a read-only turn")

    use = define_operation(
        {
            "name": "notes.confirm",
            "description": "Confirm one note.",
            "input": object_schema({"note": ref(NOTE)}),
            "output": value(object_schema({"ok": string_schema().optional()})),
            "effects": "write",
            "run": play,
        }
    )

    result = await execute(
        {
            "steps": [
                {"id": "found", "op": "notes.search", "input": {"query": "tea"}},
                {"id": "pick", "op": "notes.confirm", "input": {"note": "$found[1]"}},
            ]
        },
        [a_search_operation(), use],
    )

    assert result["ok"] is False
    assert result["steps"] == [], "the plan was refused before anything ran, not part-way"

    issue = result["issues"][0]
    assert issue["code"] == "step.write_not_allowed"
    assert issue["stepId"] == "pick", "it names the step, so the model can fix that one"
