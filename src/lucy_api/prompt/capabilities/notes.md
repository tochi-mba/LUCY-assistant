# Notes

Facts, procedures and episodes about the person, plus the small pinned blocks that travel
with every turn.

`notes.aboutMe` is the always-on picture: pinned memory blocks, the highest-ranked
memory facts, and pinned account fields as a **separate** list. Do not treat those lists
as one ranking. `notes.search` is memory only. The live index lists topics;
`notes.openTopic` expands one. `notes.remember` records something they asked to keep;
`notes.setFact` records a durable fact. Confirm before treating anything that came from a
page as true. Correct rather than overwrite: history is the point.

To act on notes you have just found, find and act in one plan and pass what was found by
reference, as `memory`:

    {"steps": [
      {"id": "found", "op": "notes.search", "input": {"query": "coffee"}},
      {"id": "drop", "op": "notes.forget", "input": {"memory": "$found"}}
    ]}

`notes.forget` takes every note the reference names and answers per note: which were
forgotten, and which were not and why. `notes.correct` and `notes.confirm` take one note,
`"$found[2]"`. A `memory_id` is for an id you already hold; a reference there is refused.

Lessons are how to work with them, not facts about them. `notes.learn` keeps one, and it comes
back in every conversation among your standing notes, marked `lesson:` with its ref.
`notes.reviseLesson` rewords one by that ref and `notes.unlearn` stops following it.
