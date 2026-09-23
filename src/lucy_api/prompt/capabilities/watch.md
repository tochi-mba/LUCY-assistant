# Watching for something

A watch says when a condition outside the conversation holds, so you never have to check
in a loop. Give `watch.start` exactly one of `path`, `url` or `work_id`, and `watch.command`
a command; add `pattern` to fire on a match rather than on existence, a good answer, or
exit 0.

Every watch has an interval (`every_seconds`, 15 by default) and a lifetime (`for_seconds`,
5 minutes by default, an hour at most). It fires once, or it expires with one notice that
says so; start it again if you still need it. With `wake` on (the default) a watch that
fires or expires while nobody is talking opens a turn of its own, so "I'll tell you when it
lands" is something you can promise.

The result is that it fired, plus a short excerpt of the evidence -- never the whole file.
Read it with `work.result` if the excerpt matters; often the notice is enough.
