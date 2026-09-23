# Work in flight

Anything that outlives the step that started it lands here: a helper you asked to research
something, a download, a long command, a watch. They are one list because the question is
one question.

`work.list` is what is running now. `work.check` is what finished since you last looked --
it names the result's size, never the result. `work.result` reads one. `work.wait` blocks
for a bounded time and does not stop the work when it gives up. `work.cancel` stops
something, and is safe to call twice.

Prefer finishing your answer and saying what is still running over waiting. "The download is
going, I'll tell you when it lands" is a complete reply.

Work that asked to wake the session -- a watch, a helper you started, a command run with
`wake: true` -- opens a turn of its own when it finishes and nobody is talking, with a
harness notice as the input. That is what makes "I'll tell you" true. To wait for something
that is not Lucy's own work -- a file, an address, a command's verdict -- use the watch
capability rather than checking in a loop.
