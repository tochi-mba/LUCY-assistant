# The tool layer

The model does not call tools one at a time. It answers with a **plan**: several steps,
where a later step names an earlier step's result by reference. The data those steps produce
never becomes tokens on its way between them.

That is the whole bet, and it is worth being concrete about what it saves. Finding a song and
playing it is, in a call-at-a-time world, a search result rendered into the context so the
model can copy an identifier out of it and type it into the next call. Here it is two steps
and a `$found[1]`. The identifier is never a token anybody paid for, and nothing was retyped,
which is where the mistakes come from.

## The model never sees a service

It sees **capabilities** with product names — `music`, `research`, `workspace`, `notes` — and
never `spotify-api`, never a port, never an HTTP verb. Behind one capability there may be one
service, three, or none.

This is not presentation. A tool surface generated from an OpenAPI document leaks all three,
and it cannot express the thing a model most needs to be told, which is when *not* to call
something. Every operation here is hand-written for that reason, and a test greps the default
prompts for service names, ports and verbs.

Operations are **workflow-shaped, not route-shaped**. `music.play` fans out to a
credential lookup, a device and a provider internally. Exposing `getTrack`, `getAlbum` and `getArtist`
instead makes the model do the joining, in context, one round trip at a time — which is
exactly the cost plans exist to avoid.

## What this looks like in practice

Every example below is a real plan shape. The model writes the plan; everything after
`→` is what comes back.

### One request, two steps, nothing retyped

*"Put on Clair de lune."*

```json
{"steps": [
  {"id": "found", "op": "music.find",
   "input": {"name": "Clair de lune", "artist": "Debussy"},
   "note": "Find the piece they asked for"},

  {"id": "play", "op": "music.play",
   "input": {"track": "$found[1]"},
   "note": "Start the best match"}
]}
```

→ `found` comes back as a collection of tracks, rendered as labelled lines. `play` receives
the first of them **as data**, resolved from the stored result, so no track identifier was
copied by hand. `"$found"` on its own would play every track the search returned, in order.

Both steps carry a `note`. That is what the person sees if the write needs approving, and
what the transcript says about the step afterwards.

### Three stores, three lists

*"What do we already know about them?"*

```json
{"steps": [
  {"id": "me", "op": "notes.aboutMe", "input": {},
   "note": "Load pinned blocks, remembered facts, and pinned account fields"}
]}
```

→ `me` comes back with **three keys**: `blocks` (memory), `facts` (ranked memories), and
`account` (pinned fields the person asked to keep in view). Those lists are not one ranking.
A later `notes.search` still queries memory only — mixing its scores with account pins
would hide a name the person asked to keep in view.

The product names stay `notes` and `account`. No host, no port, no `/v1/user`.

### Asking about a result without fetching it again

*"How many of those are things I told you, rather than things you worked out?"*

```json
{"steps": [
  {"id": "mine", "op": "notes.search",
   "input": {"query": "travel", "limit": 40},
   "note": "Find everything recorded about travel"},

  {"id": "stated", "op": "note.filter",
   "input": {"from": "$mine",
             "filters": [{"field": "trust", "op": "eq", "value": "stated"}]},
   "note": "Narrow that to the ones they told me directly"},

  {"id": "how_many", "op": "note.count",
   "input": {"from": "$stated"},
   "note": "Count them"}
]}
```

→ One network call. `note.filter` and `note.count` were generated because `notes.search`
returns notes, and they run against the stored result. Forty notes were fetched once and
never rendered twice.

`note.filter` refuses a field that was not declared, with a message naming it. That is
deliberate: filtering on a misspelled field would return everything, which looks like an
answer and is not one.

### The index, then one topic

*"What do I actually know about how they take their tea?"*

```json
{"steps": [
  {"id": "tea", "op": "notes.openTopic",
   "input": {"topic_id": "top_tea"},
   "note": "Expand the tea topic from the live index"}
]}
```

→ The live state already listed the topic. This call is the memories themselves, paid for
on purpose. Untrusted topics never appear in the index, so expanding one cannot smuggle
them in.

### Reads together, the write in the next plan

*"Check the two repositories and write me a summary."*

```json
{"steps": [
  {"id": "one", "op": "workspace.grep",
   "input": {"pattern": "TODO", "path": "service-a"},
   "note": "Find what is outstanding in the first service"},

  {"id": "two", "op": "workspace.grep",
   "input": {"pattern": "TODO", "path": "service-b"},
   "note": "Find what is outstanding in the second service"}
]}
```

→ `one` and `two` run at the same time; they are reads and independent. The summary is a
`workspace.write` whose `content` the model writes from both results, so it goes in the next
plan, once the model has read them. `content` is an ordinary field: a `$one` written there is
refused rather than resolved (see [References](#references)).

### A helper for work the parent does not need to see

*"Check both branches independently and tell me which one is actually ready."*

```json
{"steps": [
  {"id": "left", "op": "agents.spawn",
   "input": {"role": "reviewer", "group": "reviewers",
             "objective": "Read the left branch and say whether it is ready to merge."},
   "note": "Review the left branch in a clean context"},

  {"id": "right", "op": "agents.spawn",
   "input": {"role": "reviewer", "group": "reviewers",
             "objective": "Read the right branch and say whether it is ready to merge."},
   "note": "Review the right branch in a clean context"}
]}
```

`group` is optional. Helpers started under one group name are a team: when the last of them
ends, one notice names each and how it ended, `work.check` lists the group under `groups`,
and an idle session is woken once for the group rather than once a helper.

A later turn can continue a finished helper without copying the whole brief, by the handle
its spawn returned:

```json
{"steps": [
  {"id": "again", "op": "agents.reopen",
   "input": {"id": "agt_q3Vx8LmT0cRk2Hn5WbYe9sJd"},
   "note": "Continue the left review from where it stopped"}
]}
```

The new helper keeps the old objective and sees the previous items and last report. To hold
the return to a shape, pass `return_schema` on spawn or on reopen. Mail between them is hop-counted, burst-capped, and identical
unread steers count as one.

→ Each spawn returns a handle immediately. The parent keeps talking. When a helper
finishes, a notice arrives at the next tool boundary; `work.result` is how the parent
reads the capped summary. Mid-run, `agents.message` queues a steer that the helper sees
before its next round, never mid-tool; `agents.read` shows what it has done so far, and
`work.cancel` stops it. What a stopped helper did stays readable.

A helper cannot spawn another helper past the configured depth (default three), cannot
write, and cannot raise its own permission mode. Those are refusals in the tool result,
not crashes. `lucy.agent_max_concurrent` caps how many may run at once; a spawn past it
comes back `state: "queued"` with its handle and starts on its own as helpers end, and only a
spawn past a queue as long again is refused. In ask mode, the helpers one plan starts are one
approval card.

### A long command, without holding the turn open

*"Run the test suite. Tell me when it finishes."*

```json
{"steps": [
  {"id": "tests", "op": "workspace.run",
   "input": {"command": "make test", "wait": false},
   "note": "Start the suite; I will check in when it finishes"}
]}
```

→ A handle, immediately. The command keeps running. `work.check` names the state;
`work.wait` holds until it finishes or until its own deadline, and giving up does
**not** stop the command. `wait: true` (the default) still waits up to `wait_seconds`
and then returns the same handle if the command is still going. `wait_seconds: 0` is a
real deadline (return immediately with the handle), not "omit this and use the command
timeout". Add `wake: true` and a command that finishes while nobody is talking opens a
turn of its own to say so.

### A quick calculation, in one call

*"What will 1,200 be worth after seven years at 3.5%?"*

```json
{"steps": [
  {"id": "growth", "op": "workspace.script",
   "input": {"language": "python", "name": "growth",
             "code": "print(round(1200 * 1.035 ** 7, 2))\n"},
   "note": "Work out what the savings grow to"}
]}
```

→ One step and, in `ask` mode, one approval: a script is a command, so it asks under the
same permission as `workspace.run`, and the approval card shows the code. The script is
written to `.scratch/growth.py` and runs as `python3 .scratch/growth.py` from the session's
own directory, through the same machinery as `workspace.run`: the same output cap, the same
notice saying which end was kept, the same handle if it outlives its step. The result is the
command's, plus `script` (its path) and `file_fingerprint`. A named script is rewritten and
run again by the next call with that name, which is how a model corrects one; an unnamed one
is named by its own fingerprint, so the same code is the same file. `python` and `bash` are
what the sandbox has.

`.scratch/` holds an ignore file that ignores everything in it, itself included, so scratch
work never appears in `git status`, nor among the files a returning turn is told have
changed. What a script writes anywhere else is ordinary work and shows as a change like any
other. What it prints is a command's output, framed as untrusted.

### Tell me when it lands, without polling

*"Let me know when CI is green."*

```json
{"steps": [
  {"id": "ci", "op": "watch.command",
   "input": {"command": "gh run view --exit-status 123", "every_seconds": 30,
             "for_seconds": 1800, "objective": "Say when CI run 123 is green"},
   "note": "Watch the run; I will tell them when it passes"}
]}
```

→ A handle, immediately, and one approval that names the command, the interval and the
lifetime. The command runs every thirty seconds until it exits 0 or the half hour is up.
When it fires -- or expires -- and no turn is running, a turn opens with a harness notice
as its input, and Lucy tells the person. `watch.start` does the same for a workspace file
(`path`, with an optional `pattern`), a public address (`url`, `expect_status`, `pattern`)
or another piece of work (`work_id`). The result is that it fired plus a short excerpt,
never the log; `work.result` reads it when the excerpt matters.

When the thing to watch is on a repository, the sibling that can see it does the watching:

```json
{"steps": [
  {"id": "w", "op": "repos.watch",
   "input": {"repo": "octo/hello", "until": "checks_settled", "number": 42, "wake": true,
             "objective": "Merge #42 once CI is green"},
   "note": "Wait for CI on #42, then merge it"}
]}
```

→ A `subscription` handle and one approval under `repos.watch`, which also records standing
consent for the life of the watch. Nothing polls from the hub: Github-api signals when CI
settles, the session wakes, and the woken turn reads `repos.pull` and merges under the same
gate as any turn -- an approval card if merges were never allowed. It survives a hub restart
([jobs.md](jobs.md), [repos.md](repos.md)).

### Named docs, loaded on purpose

*"How do I wait on a long job without polling?"*

```json
{"steps": [
  {"id": "index", "op": "help.skills", "input": {},
   "note": "See which docs I can load"},
  {"id": "page", "op": "help.skill",
   "input": {"name": "talking", "offset": 0, "limit": 80},
   "note": "Read the talking skill before I answer"}
]}
```

→ The same corpus an MCP client loads by digest. Windowed, with a showing-count. Prefer
these over guessing how a long job, an approval, or a memory write works.

### A capability that is not connected

```json
{"steps": [
  {"id": "found", "op": "music.find",
   "input": {"name": "Clair de lune", "artist": "Debussy"},
   "note": "Find the song they asked for"},
  {"id": "play", "op": "music.play",
   "input": {"track": "$found"},
   "note": "Start it"}
]}
```

→ Nothing like this reaches the model, because an unconnected capability is **absent from
the registry**: `music.play` is not a tool it has. What it has instead is the capability
listed as needing connecting, and `capabilities.setup` to offer the link.

When a capability is connected but its credential has since been revoked, the call does run
and comes back as a result rather than an error:

```json
{"status": "connection_required",
 "service": "music",
 "profile": "personal",
 "scopes": ["user-modify-playback-state"],
 "connect_url": "…/connect?ticket=…",
 "message": "Music is not connected for profile 'personal'. Ask the person to open the
             link. Do not ask them for a password or a token."}
```

The model offers the link and carries on with the rest of the request. It does not retry, it
does not look for another route, and it does not ask for the credential itself.

### A result too large to show

```json
{"steps": [
  {"id": "log", "op": "workspace.read",
   "input": {"path": "build.log"},
   "note": "Read the build log to find why it failed"}
]}
```

→ The beginning and the end, with the middle elided and counted:

```
…
[... showing 1,190 of 41,203 tokens ...]
…
```

Head *and* tail, because errors cluster at the end of a log. To look somewhere specific, the
model runs the same step again with `show_from` set to a unique snippet it already saw.
`show_from` belongs to the step, beside `note`, not to the operation's input:

```json
{"id": "detail", "op": "workspace.read",
 "input": {"path": "build.log"},
 "show_from": "ModuleNotFoundError",
 "note": "Look at the part of the log where the import failed"}
```

Nothing was lost: the whole result is stored and still addressable. What was bounded is how
much of it became tokens.

### Reading a file, then editing it without a stale write

`workspace.read` returns numbered lines and two fingerprints. `workspace.edit` walks a
ladder (exact, whitespace, fuzzy) and refuses if `if_match` does not equal the current
file digest. The model reads in one plan and edits in the next, because the edit's text and
fingerprint are what the read showed it:

```json
{"steps": [
  {"id": "seen", "op": "workspace.read",
   "input": {"path": "dates.txt", "start_line": 1, "limit": 80},
   "note": "Read the date list before changing it"}
]}
```

```json
{"steps": [
  {"id": "fixed", "op": "workspace.edit",
   "input": {
     "path": "dates.txt",
     "old_string": "Berlin — 12 March",
     "new_string": "Berlin — 14 March",
     "if_match": "9c1d4e7a20b3f658"
   },
   "note": "Move the Berlin date by two days"}
]}
```

→ `seen` comes back as `1\t…` numbered lines plus `showing lines 1-80 of 80` and a
`file_fingerprint`, which the edit carries as `if_match`. `workspace.write` and
`workspace.patch` take `if_match` the same way. If another write landed first, `fixed` does
**not** apply: it returns
`replaced: false` and names the fix — re-read, then reapply. Ambiguous `old_string` lists
every line number it matched. A near-miss shows the closest window as a diff.

The model never edits by line number. Line numbers in the read are for the person watching,
not a handle.

### A step that fails, in a plan that carries on

```json
{"steps": [
  {"id": "hits", "op": "research.search", "input": {"query": "tour dates"},
   "note": "Look up the dates"},
  {"id": "best", "op": "hit.first", "input": {"from": "$hits"},
   "note": "Pick out the top result"},
  {"id": "note", "op": "notes.search", "input": {"query": "concerts"},
   "note": "Check what I already know about their gig preferences"}
]}
```

→ If `hits` fails, `best` is **skipped** — it depended on it — and says so. `note` still
runs, because it did not. The model gets a sentence about the failure and two useful
results, rather than nothing.

## Collections, and what declaring one buys

An operation that returns an opaque value tells the runtime nothing. It does not know the
result is a list, cannot label the entries, cannot resolve `$notes[2]`, cannot count, and can
only truncate silently. Every feature the library has is switched off by that one choice.

A **collection** is declared with three things, and each unlocks something:

| | |
| --- | --- |
| `label` | the one line an entry is rendered as. Usually all a model needs to pick which of forty things it wants. |
| `key` | a stable identity, so an entry can be named rather than described. |
| `fields` | what may be filtered, grouped and detailed by. Without it the free operations refuse outright, with a message saying so. |

`fields` is also the access boundary. A field that is not declared cannot be filtered on,
grouped by, or pulled out — so a record carrying an account id does not expose it merely by
passing through. The declarations live in one module, `packs/collections.py`, so that "does
any label expose something it should not" is a question somebody can answer by reading one
screen.

**Eight operations come free** for every collection in play: `filter`, `count`, `countBy`,
`distinct`, `mostCommon`, `first`, `pick`, `details`. They run against a **stored** result,
so *how many of those were confirmed* is arithmetic over something already fetched rather
than a second call and a second page of tokens.

They are generated only for collections something bound this turn actually produces.
Generating them for all seven would add dozens of tools a model cannot use, and selection
accuracy falls away sharply past thirty or forty. An unusable tool is not free; it is paid
for on every turn, in tokens and in wrong choices.

## References

`$stepId` is the whole result. `$stepId[1,3]` is specific positions, **1-based**, and they
index the full result rather than the lines that happened to be rendered — so a step can act
on something the model never actually read.

A field only accepts a reference if it was declared with `ref()`: the `from` of every
collection operation (`note.filter`, `hit.first`, …) and `track` on `music.play` and
`music.queue`. A reference written into any other field refuses the plan before anything
runs, naming the field, which is the right refusal: a reference is meaningful only where the
operation said it resolves one, and anywhere else it would arrive as the literal text
`$found`. There is no path into a result: `$seen.file_fingerprint` is not a reference. A value
the model needs from a result, it reads, and writes into the next plan.

## Every call says what it is for

Each step carries one plain sentence saying what *that* call is for — not a restatement of
its arguments. *"Discard the draft folder and start again"*, never
`workspace.delete(path=drafts)`.

It is written once and then pays for itself wherever a person reads about the call: the
approval card they answer, and the transcript's record of each step that ran or was refused.
Nobody can answer *"do you approve this?"* about a JSON blob. The field is Lucy's, not the
operation's: it is taken off the step before the plan runs.

When the model omits it, the approval card falls back to the gate's own sentence about the
permission. A missing note is never an error.

## Reads run together, writes run in order

Read-only steps in one plan run concurrently. Anything with `effects: "write"` runs on its
own, after what it depends on, in the order written.

A plan containing a write is **refused whole** when the turn does not allow writes — before
anything runs, not part-way through. That is what makes a read-only mode trustworthy: it is
the default, not something each caller has to remember to pass.

When the mode is `ask` and no grant covers the write, the turn parks instead of failing.
An `approval_request` item names the permission in a sentence a person can answer. The next
`input.approval` on the one write path records a grant — once, this session, this profile,
or the whole account — and re-queues the same turn. A standing answer to a permission with a
`tally` field may be limited to the values it was asked about (`"only": ["octo/hello"]`:
*always, for this repository*); a call outside the limit is asked about, not refused
([ADR-0016](adr/0016-repos-capability-and-port-8011.md)). The client's `approved: true` is an
input; the gate re-checks the ledger before the tool runs. A denial is a transcript item
and a grant the model will see as "not allowed", never an exception.

`auto` still asks for anything marked destructive or (when `approval_policy` is
`spend_and_destructive_ask`) anything that spends money. A stored grant can skip that
floor; the mode cannot. `notes.forget` and `notes.unlearn` are `notes.erase`. `workspace.delete` is
`workspace.destroy`. Those are separate from `notes.write` and `workspace.files` so
allowing ordinary writes does not also allow a delete.

## What a step is allowed to cost

| | |
| --- | --- |
| `read` | 2,000 tokens of a rendered result |
| `preview` | 400 — most of the time a model wants to know *which* of forty things it has |
| `total` | 8,000 for the whole plan's rendering |
| one result | hard-capped at 25,000 tokens before it spills |

The total bounds the **rendering**, not the data. Everything is still stored and still
addressable; what is bounded is how much of it becomes tokens. Overflow spills to the result
store and comes back as a reference, with exact counts — never dropped.

Step and plan timeouts are widened for capabilities that are legitimately slow. A long
extension job taking twelve seconds is not a bug, and failing it at ten only produces a
retry that also takes twelve.

## Failure

`failure` is `continue`: a dead service fails its own step, skips whatever depended on it,
and lets the model read a sentence about it. Four unrelated steps that already succeeded are
not thrown away.

A malformed plan comes back as **text describing what was wrong**, naming the step and the
field, and the model corrects it. Twice at most — a model that cannot answer the schema after
two tries has misunderstood the task, not the format.

The same call with the same arguments, returning the same answer twice in a row, is a model
that has lost track, not one being thorough. It is told what it already tried and what came
back, which is usually enough. What stops a model that ignores it is the turn's own budget of
rounds and calls; the operation is not withdrawn.

## A result is framed by where it came from

Every result reaches the model inside a frame that says how much weight it deserves. The pack
that produced it decides, through `result_trust(operation, data)`: Lucy's own catalogue,
settings, roster and bookkeeping are `observed`; a note is as trusted as the least trusted
memory in it, by each memory's own `trust`; a page, a file, a command's output, another
server's answer and a helper's own words are `untrusted`, whatever they say about themselves.
A result nothing marked is untrusted. The turn loop frames by the mark and knows nothing about
packs.

## Not connected is a result, not an error

A capability without a credential returns a fixed, machine-readable body: the service, the
missing scopes, a connect link, and a sentence telling the model not to ask for a password.
A model handed a 502 apologises and retries. A model handed this offers the person a link.

For Lucy's own loop an unusable capability is **absent** — not in the registry, so it cannot
be called or half-called. Over MCP the tool stays **listed** with a "needs connecting"
description, because a client that cached a tool list has no way back from one that vanished.
Different audience, different answer.

## Where this lives

| | |
| --- | --- |
| `packs/base.py` | what a capability is: states, availability, permissions, setup, and how far its results can be trusted |
| `packs/collections.py` | every collection, in one place |
| `packs/registry.py` | probing, deferral, the registry and runtime, the budgets |
| `packs/context.py` | what a handler is handed, and why no secret is reachable from it |
| `store/results.py` | the durable result store behind every reference |
