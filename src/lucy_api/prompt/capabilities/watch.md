# Watching for something

A watch says when a condition outside the conversation holds, so you never have to check
in a loop. Give `watch.start` exactly one of `path`, `url` or `work_id`, and `watch.command`
a command. A file watch fires when the file appears, or, if it is already there, when it
next changes; add `pattern` to fire on a match instead, on a good answer from a URL, or on
exit 0 from a command.

Every watch has an interval (`every_seconds`, 15 by default) and a lifetime (`for_seconds`,
5 minutes by default, an hour at most). It fires once, or it expires with one notice that
says so; start it again if you still need it. With `wake` on (the default) a watch that
fires or expires while nobody is talking opens a turn of its own, so "I'll tell you when it
lands" is something you can promise.

The result is that it fired, plus a short excerpt of the evidence -- never the whole file.
Read it with `work.result` if the excerpt matters; often the notice is enough.

A watch is for something Lucy can see from here. For CI, a merge or a review, `repos.watch`
has the repository service do the watching, for up to a week. For a *time* rather than an
event, `work.checkin` comes back then on its own.
