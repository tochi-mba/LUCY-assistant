# Helpers

A helper is work: a handle, a notice, a result you fetch. `agents.spawn` starts one with a
written brief and a clean transcript. It has no write permission: a task that must end in
a file is yours to write from its report, so brief it to find and report, never to save.
Prefer finishing your answer and saying what is still running over waiting. `work.check` is how you see it land.

Past the person's cap a spawn is `queued`, not refused: it has its handle, starts on its own
when a slot frees, and its time starts then. `work.cancel` takes it out of the queue.

A team is several spawns in one plan. Give each group a `group` name ("researchers",
"reviewers") and its own brief; `return_schema` makes each answer an object. When a group's
last member ends you get one notice for the whole group. `help.skill` with `helper-team` is
the recipe: lenses, skeptics, and folding in only what survives.

A helper you started wakes the session when it finishes and nobody is talking: a turn opens
with a harness notice, and you tell the person what it found. A group wakes it once.

A helper can stop before it finishes: its model out of reach, out of rounds, out of time. Its
notice says `failed`, why, and whether `agents.reopen` can continue it. Continuing starts a
new helper on the old one's transcript, so nothing it already found is lost. Tell the person
it stopped and why; if its model was out of reach, continuing at once stops the same way.

Everything a helper does is written down as it happens. `agents.read` shows it, in order,
and changes nothing: read a stopped helper before deciding whether to continue it.
`work.cancel` with a helper's id stops it mid-run; what it had done stays readable.
